"""
app/tools/revision.py
======================
Batch revision/QA tool: reads prompts from a CSV file (columns: id;prompt),
runs each one through the real geo pipeline with full reasoning captured at
every step, and writes:

    <outdir>/<id>.json   -- raw trace (full geometries, GeoJSON EPSG:4326, SQL,
                            candidates + scores, timings, LLM calls).
    <outdir>/<id>.html   -- readable report + Leaflet map on the UrbIS WMS base
                            map. Candidates considered = BLUE thick line + hollow
                            ring; the CHOSEN candidate = filled blue dot;
                            intermediate = orange; final geometry = red.
    <outdir>/index.html  -- links to every report.
    <outdir>/summary.md  -- COMPACT, geometry-free digest of the whole batch:
                            what the LLM decided, what was resolved, geometry
                            statistics (type, parts, points, length/area, bbox),
                            automatic flags, SQL with WKT collapsed, and --
                            if --expected is given -- your expected result next
                            to the actual one. This is the file to give to Claude.
    <outdir>/summary.json-- same digest, machine-readable, plus a coarse
                            "sketch" (>=25 m vertex spacing) of chosen/final
                            geometries so shape and extent can be judged
                            without 50 000-character WKT.

LLM: the whole run uses ONE backend, default "qwen" (= qwen2.5:32b), set
explicitly (not read from the container env) and verified before the batch with
a live call: the model that answers must be the expected one, otherwise the run
aborts. Use --llm-backend / --expect-model to change, --expect-model "" to skip.

This calls the real pipeline functions (detect/extract_entities/resolve/
resolve_address/build_sql) directly -- not through MCP/JSON-RPC -- so it
needs to run where app/ is importable and your .env-configured DB/LLM are
reachable, e.g.:

    podman exec geo-mcp python -m app.tools.revision \\
        /srv/resources/urbis/prompts.csv \\
        --expected /srv/resources/urbis/expectations.csv \\
        --outdir /srv/resources/urbis/revision \\
        --table unassigned_assets

CSV format: semicolon-separated, UTF-8, header row required ("id;prompt").
If a prompt itself contains a semicolon, wrap it in double quotes.
Expectations CSV (optional): "id;expected", same rules.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

import app.config as _config
import app.geo.detector as _detector
import app.llm as _llm
from app.config import (
    DEFAULT_AUTONOMY_LEVEL,
    DEFAULT_BUFFER_METERS,
    DISAMBIGUATION_MARGIN,
    EMBEDDING_BACKENDS,
    LLM_BACKENDS,
    LEVEL_1_CONFIRM_ALL,
    LEVEL_2_CONFIRM_RISKY,
    ON_BUFFER_METERS,
    SRID,
)
from app.geo.builder import _validate_identifier
from app.geo.detector import detect, extract_entities
from app.geo.mapviz import plan_geometry, wkts_to_geojson
from app.tools.geo_tools import AREA_TYPES, _short_name_probe, plan_from_extraction

LOW_CONFIDENCE_SCORE = 0.5
DEFAULT_LLM_BACKEND = "qwen"
DEFAULT_EXPECT_MODEL = "qwen2.5:32b"
SKETCH_SPACING_M = 25.0        # min vertex spacing of the coarse sketches in summary.json
SKETCH_MAX_POINTS = 200        # cap per sketched geometry


# --------------------------------------------------------------------------- #
# LLM selection / verification / call tracing
# --------------------------------------------------------------------------- #

_LLM_CALLS: list[dict] = []
_PURPOSE = {"v": None}


def install_llm_trace() -> None:
    """Wrap the detector's llm_json so every call (purpose, seconds, parsed output)
    is captured for the report. The wrapped function is the real one."""
    orig = _detector.llm_json

    def traced(prompt, *args, **kwargs):
        t0 = time.perf_counter()
        out, err = None, None
        try:
            out = orig(prompt, *args, **kwargs)
            return out
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _LLM_CALLS.append({"purpose": _PURPOSE["v"], "seconds": round(time.perf_counter() - t0, 2),
                               "output": out, "error": err})

    _detector.llm_json = traced


def select_and_verify_llm(backend: str, expect_model: str) -> dict:
    """Force ONE backend for the whole run and prove it is the right model.

    1. app.llm / app.config ACTIVE_LLM_BACKEND are set explicitly (the container's
       GEO_MCP_LLM_BACKEND is ignored for this run).
    2. The configured model must equal expect_model.
    3. A live call must be answered by expect_model (Ollama reports the model name),
       which also loads/warms it before the first timed prompt.
    Raises SystemExit with a clear message on any mismatch."""
    if backend not in LLM_BACKENDS:
        raise SystemExit(f"Unknown --llm-backend {backend!r}; available: {sorted(LLM_BACKENDS)}")
    cfg = LLM_BACKENDS[backend]
    _llm.ACTIVE_LLM_BACKEND = backend
    _config.ACTIVE_LLM_BACKEND = backend
    info = {"backend": backend, "configured_model": cfg["model"], "base_url": cfg["base_url"],
            "embedding_backend": _llm.ACTIVE_EMBEDDING_BACKEND,
            "embedding_model": EMBEDDING_BACKENDS.get(_llm.ACTIVE_EMBEDDING_BACKEND, {}).get("model"),
            "container_env_backend": _config.os.environ.get("GEO_MCP_LLM_BACKEND") if hasattr(_config, "os") else None}
    if expect_model and cfg["model"] != expect_model:
        raise SystemExit(f"ABORT: backend {backend!r} is configured as model {cfg['model']!r}, expected {expect_model!r}.")
    try:
        r = requests.post(
            f"{cfg['base_url']}/api/generate",
            json={"model": cfg["model"], "prompt": 'Return exactly this JSON: {"ok": true}', "stream": False,
                  "format": "json", "options": {"temperature": 0}, "keep_alive": "30m"},
            timeout=600,
        )
        r.raise_for_status()
        j = r.json()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"ABORT: LLM preflight call to {cfg['base_url']} failed: {exc}")
    info["served_model"] = j.get("model")
    info["preflight_load_s"] = round((j.get("load_duration") or 0) / 1e9, 2)
    if expect_model and info["served_model"] != expect_model:
        raise SystemExit(f"ABORT: preflight was answered by {info['served_model']!r}, expected {expect_model!r}.")
    try:  # best effort: what else is resident?
        ps = requests.get(f"{cfg['base_url']}/api/ps", timeout=10).json().get("models", [])
        names = [m.get("model") or m.get("name") for m in ps]
        info["resident_models"] = names
        info["other_llms_resident"] = [n for n in names if n != cfg["model"]
                                       and (n or "").split(":")[0] not in {"bge-m3", "all-minilm", "mxbai-embed-large",
                                                                           "nomic-embed-text", "snowflake-arctic-embed"}]
    except Exception:  # noqa: BLE001
        info["resident_models"] = None
        info["other_llms_resident"] = None
    return info


# --------------------------------------------------------------------------- #
# Traced pipeline -- same logic as app/tools/geo_tools.py::geo_pipeline, but
# instrumented to capture every intermediate artifact for the report/map
# instead of only the final SQL.
# --------------------------------------------------------------------------- #

def run_traced_pipeline(
    prompt_id: str,
    prompt: str,
    table: str = "potholes",
    geom_column: str = "geom",
    srid: int = SRID,
    project: Optional[str] = None,
    autonomy_level: int = DEFAULT_AUTONOMY_LEVEL,
    buffer_meters: Optional[float] = None,
    surface_source: str = "urbis.streetsurfaces",
    surface_geom_column: str = "geometry",
) -> dict:
    started = time.perf_counter()
    record: dict = {"id": prompt_id, "prompt": prompt, "error": None, "timings": {}, "srid": srid,
                    "llm": {"backend": _llm.ACTIVE_LLM_BACKEND,
                            "model": LLM_BACKENDS.get(_llm.ACTIVE_LLM_BACKEND, {}).get("model")}}
    _LLM_CALLS.clear()
    timings = record["timings"]

    try:
        table = _validate_identifier(table, "table")
        geom_column = _validate_identifier(geom_column, "geom_column")

        _PURPOSE["v"] = "detect"
        t0 = time.perf_counter()
        intent = detect(prompt)
        timings["detect_s"] = round(time.perf_counter() - t0, 2)
        record["detect"] = intent

        extraction = None
        if not intent.get("has_spatial"):
            extraction = _short_name_probe(prompt)
            if extraction is not None:
                record["gate_override"] = True
        if extraction is None and not intent.get("has_spatial"):
            record.update(
                extraction=None,
                entities=[],
                relationship="NONE",
                distance_meters=None,
                used_default_distance=False,
                intermediate_geometries=[],
                final_geometries=[],
                sql=f"SELECT * FROM {table}",
                autonomy_level=autonomy_level,
                needs_confirmation=False,
                ambiguous_entities=[],
            )
            record["llm_calls"] = list(_LLM_CALLS)
            record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            return record

        if extraction is None:
            _PURPOSE["v"] = "extract"
            t0 = time.perf_counter()
            extraction = extract_entities(prompt)
            timings["extract_s"] = round(time.perf_counter() - t0, 2)
        record["extraction"] = extraction

        def _enrich(rec: dict) -> None:
            # GeoJSON of every candidate, for the map (same data the production result carries as WKT)
            cands = rec["candidates"]
            cand_wkts = [c.get("wkt") or c.get("geometry") for c in cands]
            cand_geojson = wkts_to_geojson(cand_wkts, srid)
            gj_by_wkt = dict(zip([w for w in cand_wkts if w], cand_geojson))
            for c, w in zip(cands, cand_wkts):
                c["geojson"] = gj_by_wkt.get(w)

        t0 = time.perf_counter()
        result = plan_from_extraction(
            prompt, table, extraction, geom_column=geom_column, srid=srid, project=project,
            autonomy_level=autonomy_level, buffer_meters=buffer_meters, on_entity=_enrich,
        )
        timings["resolve_and_plan_s"] = round(time.perf_counter() - t0, 2)

        intermediate: list[dict] = []
        final_geoms: list[dict] = []
        geometry_info = None
        if result.get("plan"):
            t_geo = time.perf_counter()
            geometry_info = plan_geometry(result["plan"])      # urbis.geo_plan: the very function the SQL calls
            timings["geometry_s"] = round(time.perf_counter() - t_geo, 2)
            if geometry_info["path"]:
                intermediate.append({"label": "route axes (path through the street network)", "geojson": geometry_info["path"]})
            if geometry_info["target"]:
                label = f"target geometry ({geometry_info['mode']})"
                if result.get("distance_meters"):
                    label += f", buffer {result['distance_meters']:g} m"
                final_geoms.append({"label": label, "geojson": geometry_info["target"]})

        record.update(
            result,
            intermediate_geometries=intermediate,
            final_geometries=final_geoms,
            geometry_mode=(geometry_info or {}).get("mode"),
            geometry_note=(geometry_info or {}).get("note"),
        )

    except Exception as exc:  # noqa: BLE001
        record["error"] = f"{type(exc).__name__}: {exc}"

    record["llm_calls"] = list(_LLM_CALLS)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return record


# --------------------------------------------------------------------------- #
# HTML report rendering
# --------------------------------------------------------------------------- #

_HTML_HEAD = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>geo-mcp revision -- {id}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  body {{ font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 0; padding: 0; color: #1a1a1a; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  .prompt {{ font-size: 15px; background: #f4f4f4; padding: 12px 16px; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
  th {{ background: #2f5496; color: white; }}
  tr.chosen {{ background: #eaf6ea; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }}
  .badge.ok {{ background: #d6f5d6; color: #175c17; }}
  .badge.confirm {{ background: #fff2cc; color: #7a5b00; }}
  .badge.error {{ background: #f8d7da; color: #7a1c24; }}
  .badge.none {{ background: #e2e2e2; color: #444; }}
  code, pre {{ font-family: Consolas, monospace; font-size: 13px; }}
  pre {{ background: #f2f2f2; padding: 12px; border-radius: 6px; overflow-x: auto; }}
  .legend span {{ display: inline-block; margin-right: 16px; font-size: 13px; }}
  .legend i {{ display: inline-block; width: 12px; height: 12px; margin-right: 4px; vertical-align: middle; border-radius: 2px; }}
  #map {{ height: 480px; border-radius: 6px; border: 1px solid #ccc; margin-top: 8px; }}
  .warn {{ background: #fff2cc; padding: 10px 14px; border-radius: 6px; margin: 10px 0; }}
  .err {{ background: #f8d7da; padding: 10px 14px; border-radius: 6px; margin: 10px 0; }}
  .meta {{ color: #666; font-size: 13px; }}
</style>
</head>
<body>
<div class="wrap">
"""

_HTML_TAIL = """
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const CANDIDATES = {candidates_geojson};
const INTERMEDIATE = {intermediate_geojson};
const FINAL = {final_geojson};
const CHOSEN_DOTS = {chosen_dots_geojson};
const BLUE = '#1f5fd6';

const map = L.map('map');
L.tileLayer.wms('https://geoservices-urbis.irisnet.be/geoserver/BaseMaps/ows', {{ layers: 'UrbISFrenchDutchLabeledColor', format: 'image/png', version: '1.3.0', attribution: '© Paradigm / CIRB-CIBG (UrbIS)' }}).addTo(map);

const layers = [];

function addLayer(fc, style, popupFn) {{
    if (!fc || !fc.features || fc.features.length === 0) return null;
    const layer = L.geoJSON(fc, {{
        style: style,
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{ radius: 6, ...style }}),
        onEachFeature: (feature, lyr) => {{
            if (popupFn) lyr.bindPopup(popupFn(feature.properties || {{}}));
        }},
    }});
    layers.push(layer);
    return layer;
}}

const candidatesLayer = (CANDIDATES.features && CANDIDATES.features.length) ? L.geoJSON(CANDIDATES, {{
    style: (f) => ({{ color: BLUE, weight: 4, opacity: 0.9, fill: !!(f.properties && f.properties.chosen), fillColor: BLUE, fillOpacity: 0.15 }}),
    pointToLayer: (f, ll) => L.circleMarker(ll, {{ radius: 10, color: BLUE, weight: 4, fill: false }}),
    onEachFeature: (f, lyr) => {{
        const p = f.properties || {{}};
        lyr.bindPopup(`<b>${{p.name || '(unnamed)'}}</b><br>entity: ${{p.entity || ''}}<br>id: ${{p.id !== undefined ? p.id : ''}}<br>score: ${{typeof p.score === 'number' ? p.score.toFixed(2) : '?'}}${{p.chosen ? '<br><i>chosen</i>' : ''}}`);
    }},
}}) : null;
if (candidatesLayer) layers.push(candidatesLayer);
const intermediateLayer = addLayer(
    INTERMEDIATE,
    {{ color: '#e67e22', weight: 2, dashArray: '6,4', fillOpacity: 0.08 }},
    (p) => `<b>${{p.label || 'intermediate'}}</b>`
);
const finalLayer = addLayer(
    FINAL,
    {{ color: '#c0392b', weight: 3, fillOpacity: 0.15 }},
    (p) => `<b>${{p.label || 'final geometry'}}</b>`
);

const dotsLayer = (CHOSEN_DOTS.features && CHOSEN_DOTS.features.length) ? L.geoJSON(CHOSEN_DOTS, {{
    pointToLayer: (f, ll) => L.circleMarker(ll, {{ radius: 9, color: '#ffffff', weight: 2, fillColor: BLUE, fillOpacity: 1 }})
        .bindPopup(`<b>chosen:</b> ${{(f.properties || {{}}).name || ''}}<br>entity: ${{(f.properties || {{}}).entity || ''}}`),
}}) : null;
if (dotsLayer) layers.push(dotsLayer);

const allLayers = L.featureGroup(layers.filter(Boolean));
if (layers.length > 0) {{
    map.fitBounds(allLayers.getBounds().pad(0.2));
}} else {{
    map.setView([50.8503, 4.3517], 12); // Brussels fallback
}}

const overlays = {{}};
if (candidatesLayer) overlays['Candidates considered (blue)'] = candidatesLayer.addTo(map);
if (intermediateLayer) overlays['Intermediate geometry'] = intermediateLayer.addTo(map);
if (finalLayer) overlays['Final geometry'] = finalLayer.addTo(map);
if (dotsLayer) overlays['Chosen candidate (dot)'] = dotsLayer.addTo(map);
L.control.layers(null, overlays).addTo(map);
</script>
</body>
</html>
"""


def _esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def _status_badge(record: dict) -> str:
    if record.get("error"):
        return '<span class="badge error">ERROR</span>'
    if record.get("relationship") == "NONE":
        return '<span class="badge none">NO SPATIAL CONTEXT</span>'
    if record.get("needs_confirmation"):
        return '<span class="badge confirm">NEEDS CONFIRMATION</span>'
    return '<span class="badge ok">OK</span>'


def _candidates_table(entity: dict) -> str:
    rows = []
    chosen_id = (entity.get("chosen") or {}).get("id") or (entity.get("chosen") or {}).get("inspire_id")
    for c in entity.get("candidates", []):
        cid = c.get("id") or c.get("inspire_id")
        is_chosen = cid == chosen_id
        name = c.get("name_fr") or c.get("name_nl") or c.get("street_name_fr") or c.get("street_name_nl") or "(unnamed)"
        score = c.get("score")
        rows.append(
            f'<tr class="{"chosen" if is_chosen else ""}">'
            f"<td>{_esc(cid)}</td><td>{_esc(name)}</td>"
            f"<td>{score:.3f}</td>"
            f'<td>{"&#10003; chosen" if is_chosen else ""}</td></tr>'
        )
    if not rows:
        return "<p><i>No candidates found.</i></p>"
    return (
        "<table><tr><th>id</th><th>name</th><th>score</th><th></th></tr>"
        + "".join(rows)
        + "</table>"
    )


def render_html(record: dict) -> str:
    parts = [_HTML_HEAD.format(id=_esc(record["id"]))]
    parts.append(f"<h1>Revision report -- id {_esc(record['id'])} {_status_badge(record)}</h1>")
    parts.append(f'<p class="meta">elapsed: {record.get("elapsed_seconds", "?")}s</p>')
    parts.append(f'<div class="prompt">{_esc(record["prompt"])}</div>')

    if record.get("error"):
        parts.append(f'<div class="err"><b>Error:</b> {_esc(record["error"])}</div>')

    parts.append("<h2>1. Spatial detection</h2>")
    parts.append(f"<pre>{_esc(json.dumps(record.get('detect'), indent=2))}</pre>")

    if record.get("relationship") == "NONE":
        parts.append("<p>No spatial context detected -- pipeline used an unfiltered query.</p>")
    else:
        parts.append("<h2>2. Entity extraction</h2>")
        parts.append(f"<pre>{_esc(json.dumps(record.get('extraction'), indent=2))}</pre>")

        parts.append("<h2>3. Entity resolution</h2>")
        for e in record.get("entities", []):
            parts.append(f"<h3>&ldquo;{_esc(e['text'])}&rdquo; ({_esc(e.get('type'))}, role={_esc(e.get('role'))})</h3>")
            parts.append(_candidates_table(e))

        if record.get("ambiguous_entities"):
            parts.append('<div class="warn"><b>Ambiguous:</b> multiple close-scoring candidates for: '
                          + ", ".join(_esc(a["entity"]) for a in record["ambiguous_entities"]) + "</div>")

        parts.append("<h2>4. Relationship &amp; geometry</h2>")
        parts.append(
            f"<p><b>Relationship:</b> {_esc(record.get('relationship'))}<br>"
            f"<b>Distance used:</b> {_esc(record.get('distance_meters'))}"
            f" {'(default, not stated in prompt)' if record.get('used_default_distance') else '(stated or overridden)'}<br>"
            f"<b>Autonomy level:</b> {_esc(record.get('autonomy_level'))} &rarr; "
            f"{'confirmation required before running' if record.get('needs_confirmation') else 'ready to run'}</p>"
        )

        parts.append("<h2>5. SQL produced</h2>")
        parts.append(f"<pre>{_esc(record.get('sql'))}</pre>")

    parts.append("<h2>6. Map</h2>")
    parts.append(
        '<div class="legend">'
        '<span><i style="background:#fff;border:3px solid #1f5fd6;"></i>Candidate considered</span>'
        '<span><i style="background:#1f5fd6;border-radius:50%;"></i>Chosen candidate</span>'
        '<span><i style="background:#e67e22;"></i>Intermediate geometry</span>'
        '<span><i style="background:#c0392b;"></i>Final geometry</span>'
        "</div>"
    )
    parts.append('<div id="map"></div>')

    candidates_features = []
    for e in record.get("entities", []):
        chosen_id = (e.get("chosen") or {}).get("id") or (e.get("chosen") or {}).get("inspire_id")
        for c in e.get("candidates", []):
            gj = c.get("geojson")
            if not gj:
                continue
            cid = c.get("id") or c.get("inspire_id")
            name = c.get("name_fr") or c.get("name_nl") or c.get("street_name_fr") or "(unnamed)"
            is_chosen = cid == chosen_id and c.get("wkt") == (e.get("chosen") or {}).get("wkt")
            candidates_features.append({
                "type": "Feature",
                "geometry": gj,
                "properties": {"entity": e["text"], "name": name, "id": cid, "score": c.get("score"), "chosen": is_chosen},
            })

    chosen_dots = []
    for e in record.get("entities", []):
        ch = e.get("chosen")
        if not ch:
            continue
        gj, dot = ch.get("geojson"), None
        if gj and gj.get("type") == "Point":
            dot = gj
        else:
            try:
                if ch.get("centroid_wkt"):
                    dot = (wkts_to_geojson([ch["centroid_wkt"]], record.get("srid", SRID)) or [None])[0]
            except Exception:  # noqa: BLE001
                dot = None
            if not dot and gj:
                st = gj_stats(gj)
                dot = {"type": "Point", "coordinates": st["center"]} if st and st.get("center") else None
        if dot:
            chosen_dots.append({"type": "Feature", "geometry": dot,
                                "properties": {"entity": e["text"], "name": _cand_name(ch)}})

    intermediate_features = [
        {"type": "Feature", "geometry": g["geojson"], "properties": {"label": g["label"]}}
        for g in record.get("intermediate_geometries", []) if g.get("geojson")
    ]
    final_features = [
        {"type": "Feature", "geometry": g["geojson"], "properties": {"label": g["label"]}}
        for g in record.get("final_geometries", []) if g.get("geojson")
    ]

    parts.append(
        _HTML_TAIL.format(
            candidates_geojson=json.dumps({"type": "FeatureCollection", "features": candidates_features}),
            intermediate_geojson=json.dumps({"type": "FeatureCollection", "features": intermediate_features}),
            final_geojson=json.dumps({"type": "FeatureCollection", "features": final_features}),
            chosen_dots_geojson=json.dumps({"type": "FeatureCollection", "features": chosen_dots}),
        )
    )
    return "".join(parts)


def render_index(records: list[dict], flags_by_id: Optional[dict] = None) -> str:
    flags_by_id = flags_by_id or {}
    rows = []
    for r in records:
        rows.append(
            f'<tr><td><a href="{_esc(r["id"])}.html">{_esc(r["id"])}</a></td>'
            f"<td>{_esc(r['prompt'])}</td>"
            f"<td>{_esc(r.get('relationship'))}</td>"
            f"<td>{_status_badge(r)}</td>"
            f"<td>{_esc(', '.join(flags_by_id.get(str(r['id']), [])))}</td>"
            f"<td>{r.get('elapsed_seconds', '?')}s</td></tr>"
        )
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>geo-mcp revision batch</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px;}"
        "table{border-collapse:collapse;width:100%;} th,td{border:1px solid #ddd;padding:6px 10px;"
        "text-align:left;} th{background:#2f5496;color:white;}</style></head><body>"
        f"<h1>geo-mcp revision batch ({len(records)} prompts)</h1>"
        "<p><a href='summary.md'>summary.md</a> &middot; <a href='summary.json'>summary.json</a> (compact digest for review)</p>"
        "<table><tr><th>id</th><th>prompt</th><th>relationship</th><th>status</th><th>flags</th><th>time</th></tr>"
        + "".join(rows)
        + "</table></body></html>"
    )


# --------------------------------------------------------------------------- #
# Compact, geometry-free digest for review (summary.md / summary.json)
# --------------------------------------------------------------------------- #

_WKT_RE = re.compile(r"'((?:SRID=\d+;)?\s*(?:MULTI)?(?:POINT|LINESTRING|POLYGON|GEOMETRYCOLLECTION)[^']*)'", re.I)


def collapse_sql(sql: Optional[str], limit: int = 1800) -> Optional[str]:
    """SQL with every WKT literal replaced by a short placeholder."""
    if not sql:
        return None

    def _sub(m: "re.Match") -> str:
        g = m.group(1)
        t = re.match(r"(?:SRID=\d+;)?\s*([A-Za-z]+)", g)
        return f"'<WKT {(t.group(1) if t else 'GEOM').upper()} {len(g)}c>'"

    out = re.sub(r"\s+", " ", _WKT_RE.sub(_sub, sql)).strip()
    return out if len(out) <= limit else out[:limit] + f" ... [+{len(out) - limit} chars]"


def _iter_pts(coords):
    if coords and isinstance(coords[0], (int, float)):
        yield coords
    else:
        for c in coords or []:
            yield from _iter_pts(c)


def _mdeg(lat: float) -> tuple[float, float]:
    return 111320.0 * math.cos(math.radians(lat)), 110574.0  # metres per degree lon, lat


def _ring_area_m2(ring) -> float:
    if len(ring) < 4:
        return 0.0
    mx, my = _mdeg(ring[0][1])
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        s += (x1 * mx) * (y2 * my) - (x2 * mx) * (y1 * my)
    return abs(s) / 2.0


def _geoms(gj: dict) -> list[dict]:
    if gj.get("type") == "GeometryCollection":
        return [g for sub in gj.get("geometries", []) for g in _geoms(sub)]
    return [gj]


def gj_stats(gj: Optional[dict]) -> Optional[dict]:
    """Type, parts, points, bbox, size, approx. area/length of a GeoJSON geometry (EPSG:4326)."""
    if not gj:
        return None
    pts = [p for g in _geoms(gj) for p in _iter_pts(g.get("coordinates"))]
    if not pts:
        return {"type": gj.get("type"), "parts": 0, "points": 0}
    lons, lats = [p[0] for p in pts], [p[1] for p in pts]
    mx, my = _mdeg(sum(lats) / len(lats))
    area = length = 0.0
    parts = 0
    for g in _geoms(gj):
        t, c = g.get("type"), g.get("coordinates")
        if t == "Point":
            parts += 1
        elif t == "LineString":
            parts += 1
            length += sum(math.hypot((b[0] - a[0]) * mx, (b[1] - a[1]) * my) for a, b in zip(c, c[1:]))
        elif t == "MultiLineString":
            parts += len(c)
            for ln in c:
                length += sum(math.hypot((b[0] - a[0]) * mx, (b[1] - a[1]) * my) for a, b in zip(ln, ln[1:]))
        elif t == "Polygon":
            parts += 1
            area += _ring_area_m2(c[0]) - sum(_ring_area_m2(h) for h in c[1:])
        elif t == "MultiPolygon":
            parts += len(c)
            for poly in c:
                area += _ring_area_m2(poly[0]) - sum(_ring_area_m2(h) for h in poly[1:])
    out = {
        "type": gj.get("type"), "parts": parts, "points": len(pts),
        "bbox": [round(min(lons), 5), round(min(lats), 5), round(max(lons), 5), round(max(lats), 5)],
        "bbox_m": [round((max(lons) - min(lons)) * mx), round((max(lats) - min(lats)) * my)],
        "center": [round((min(lons) + max(lons)) / 2, 5), round((min(lats) + max(lats)) / 2, 5)],
    }
    if area:
        out["area_m2"] = round(area)
    if length:
        out["length_m"] = round(length)
    return out


def _paths(gj: dict) -> list[list[list[float]]]:
    """Polygons -> their exterior rings as lines; lines stay lines; points stay 1-point paths."""
    out: list[list[list[float]]] = []
    for g in _geoms(gj):
        t, c = g.get("type"), g.get("coordinates")
        if t == "Point":
            out.append([c])
        elif t == "LineString":
            out.append(c)
        elif t == "MultiLineString":
            out.extend(c)
        elif t == "Polygon":
            out.append(c[0])
        elif t == "MultiPolygon":
            out.extend(poly[0] for poly in c)
    return out


def _decimate(path: list[list[float]], spacing_m: float) -> list[list[float]]:
    if len(path) <= 2:
        return path
    mx, my = _mdeg(path[0][1])
    kept = [path[0]]
    for p in path[1:-1]:
        if math.hypot((p[0] - kept[-1][0]) * mx, (p[1] - kept[-1][1]) * my) >= spacing_m:
            kept.append(p)
    kept.append(path[-1])
    return kept


def sketch(gj: Optional[dict], spacing_m: float = SKETCH_SPACING_M, cap: int = SKETCH_MAX_POINTS) -> Optional[dict]:
    """Coarse line sketch: polygons become their exterior outline, with at most one vertex
    per `spacing_m` metres (spacing is widened automatically until <= `cap` points)."""
    if not gj:
        return None
    paths = _paths(gj)
    if not paths:
        return None
    if all(len(p) == 1 for p in paths):
        return {"type": "MultiPoint", "coordinates": [[round(p[0][0], 5), round(p[0][1], 5)] for p in paths[:cap]]}
    sp = spacing_m
    while True:
        dec = [_decimate(p, sp) for p in paths]
        total = sum(len(p) for p in dec)
        if total <= cap or sp > 20000:
            break
        sp *= 1.6
    return {"type": "MultiLineString", "min_vertex_spacing_m": round(sp),
            "coordinates": [[[round(x, 5), round(y, 5)] for x, y in p] for p in dec]}


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


def _in_prompt(text: str, prompt: str) -> bool:
    """True if the extracted entity text is (approximately) present in the prompt."""
    from difflib import SequenceMatcher
    t, p = _norm(text), _norm(prompt)
    if not t or t in p:
        return True
    words, n = p.split(), max(len(t.split()), 1)
    best = 0.0
    for size in {max(n - 1, 1), n, n + 1}:
        for i in range(max(len(words) - size + 1, 1)):
            best = max(best, SequenceMatcher(None, t, " ".join(words[i:i + size])).ratio())
    return best >= 0.8


def _cand_id(c: dict):
    return c.get("id") if c.get("id") is not None else c.get("inspire_id")


def _cand_name(c: dict) -> str:
    n = c.get("name_fr") or c.get("name_nl")
    if n:
        return n
    s = c.get("street_name_fr") or c.get("street_name_nl")
    if s:
        return f"{s} {c.get('policenum') or ''}{(' bte ' + str(c['boxnumber'])) if c.get('boxnumber') else ''} ({c.get('municipality_name_fr') or ''})".strip()
    return "(unnamed)"


def _cand_type(c: dict) -> str:
    return c.get("type") or ("ADDRESS" if c.get("policenum") is not None or c.get("street_name_fr") else "?")


def compute_flags(r: dict) -> list[dict]:
    """Automatic red flags, each tied to a pattern seen in earlier revision batches."""
    fl: list[dict] = []
    add = lambda code, detail: fl.append({"code": code, "detail": detail})  # noqa: E731
    det, ex = r.get("detect") or {}, r.get("extraction") or {}
    rel = r.get("relationship")
    if r.get("error"):
        add("ERROR", str(r["error"])[:300])
    for c in r.get("llm_calls", []):
        if c.get("error"):
            add("LLM_ERROR", f"{c.get('purpose')}: {c['error'][:200]}")
    if r.get("gate_override"):
        add("GATE_OVERRIDE", "the intent LLM said 'no spatial context'; a short-name probe matched a gazetteer name and the prompt was treated as a place")
    for n in r.get("notes", []) or []:
        if "does not exist on" in n or "no address" in n:
            add("ADDRESS_NUMBER_NOT_FOUND", n)
        elif "not in the prompt" in n:
            add("ENTITY_REPAIRED", n)
    mode = r.get("geometry_mode") or ""
    if mode.startswith("fallback") or mode in ("route_fallback_unscoped", "street_no_tram", "anchors_touch"):
        add("GEOMETRY_" + mode.upper(), r.get("geometry_note") or mode)
    if r.get("status") == "unresolved_entity":
        add("UNRESOLVED_ENTITY", f"no candidate for: {r.get('unresolved_entities')}")
    elif det.get("has_spatial") and not r.get("final_geometries") and not r.get("error"):
        add("NO_FINAL_GEOMETRY", f"relationship={rel}: nothing would be selected on the map")
    ents = r.get("entities", []) or []
    prompt = r.get("prompt", "")
    for e in ents:
        t, ch, cands = e.get("text", ""), e.get("chosen"), e.get("candidates", []) or []
        if not cands:
            add("NO_CANDIDATES", f"“{t}”")
            continue
        if not _in_prompt(t, prompt):
            add("ENTITY_NOT_IN_PROMPT", f"“{t}” was extracted by the LLM but does not appear in the prompt (invented/translated?)")
        sc = ch.get("score") if ch else None
        if isinstance(sc, (int, float)) and sc < 0.6:
            add("LOW_SCORE", f"“{t}” -> {_cand_name(ch)} score {sc:.2f}")
        et, ct = e.get("type"), _cand_type(ch) if ch else None
        if ch and et not in (None, "ANY") and ct not in (None, "?") and et != ct:
            add("TYPE_MISMATCH", f"“{t}” extracted as {et} but resolved to {ct} “{_cand_name(ch)}”")
        if ch and et != "ADDRESS":
            same = [c for c in cands if _norm(_cand_name(c)) == _norm(_cand_name(ch)) and abs((c.get("score") or 0) - (sc or 0)) < 0.011]
            if len(same) > 1:
                add("MULTI_SEGMENT_NAME",
                    f"“{t}”: {len(same)} candidates share the name “{_cand_name(ch)}” (street segments?) but only the first "
                    f"is used{' -- list is capped at ' + str(len(cands)) + ', more may exist' if len(cands) >= 3 else ''}")
        if ch and et == "ADDRESS":
            req, got = ch.get("requested_number"), ch.get("matched_number")
            if req and got and str(req).upper() != str(got).upper():
                add("ADDRESS_NUMBER_MISMATCH", f"“{t}”: requested number {req} but matched house number {got} on {_cand_name(ch)}")
            if ch.get("address_fallback"):
                add("ADDRESS_FELL_BACK_TO_STREET", f"“{t}”: no address found, the street geometry is used")
    if rel in ("BETWEEN", "FROM_TO"):
        roles = [e.get("role") for e in ents]
        anchors = [e for e in ents if e.get("role") in ("start", "via", "end")]
        if len(anchors) > 2 and not any(e.get("role") == "via" for e in ents):
            add("BETWEEN_WAYPOINT_CHAIN", f"{len(anchors)} route anchors (roles {roles}) but no 'via'")
        wk = [(e.get("chosen") or {}).get("wkt") for e in ents]
        for i in range(len(wk)):
            for j in range(i + 1, len(wk)):
                if wk[i] and wk[i] == wk[j]:
                    add("START_EQUALS_END", f"entities {i + 1} and {j + 1} resolved to the identical geometry")
    return fl


def _status_text(r: dict) -> str:
    if r.get("error"):
        return "ERROR"
    if not (r.get("detect") or {}).get("has_spatial"):
        return "NO_SPATIAL"
    if r.get("status") == "unresolved_entity":
        return "UNRESOLVED"
    if r.get("relationship") == "NONE":
        return "REL_NONE"
    return "NEEDS_CONFIRMATION" if r.get("needs_confirmation") else "OK"


def digest_record(r: dict, expected: Optional[str], spacing_m: float) -> dict:
    ex = r.get("extraction") or {}
    ents = []
    for e in r.get("entities", []) or []:
        ch = e.get("chosen")
        cid = _cand_id(ch) if ch else None
        cands = []
        for c in (e.get("candidates") or [])[:6]:
            st = gj_stats(c.get("geojson"))
            cands.append({
                "id": _cand_id(c), "name": _cand_name(c), "type": _cand_type(c),
                "score": round(c["score"], 3) if isinstance(c.get("score"), (int, float)) else None,
                "geometry": ({k: st[k] for k in ("type", "parts", "points", "area_m2", "length_m") if k in st} if st else None),
                "chosen": bool(ch) and _cand_id(c) == cid and c.get("wkt") == ch.get("wkt"),
            })
        ents.append({"text": e.get("text"), "type": e.get("type"), "role": e.get("role"),
                     "n_candidates": len(e.get("candidates") or []), "candidates": cands,
                     "chosen_sketch": sketch(ch.get("geojson"), spacing_m) if ch else None,
                     "chosen_stats": gj_stats(ch.get("geojson")) if ch else None})
    fin = [{"label": g.get("label"), "stats": gj_stats(g.get("geojson")), "sketch": sketch(g.get("geojson"), spacing_m)}
           for g in r.get("final_geometries", []) or []]
    mid = [{"label": g.get("label"), "stats": gj_stats(g.get("geojson")), "sketch": sketch(g.get("geojson"), spacing_m)}
           for g in r.get("intermediate_geometries", []) or []]
    return {
        "id": r["id"], "prompt": r["prompt"], "expected": expected, "status": _status_text(r), "error": r.get("error"),
        "llm": r.get("llm"), "detect": r.get("detect"),
        "extraction": {"relationship": ex.get("relationship"), "distance_meters": ex.get("distance_meters"),
                       "entities": [{"text": e.get("text"), "type": e.get("type"), "role": e.get("role")} for e in ex.get("entities", [])]} if ex else None,
        "relationship": r.get("relationship"), "distance_meters": r.get("distance_meters"),
        "used_default_distance": r.get("used_default_distance"), "needs_confirmation": r.get("needs_confirmation"),
        "entities": ents, "intermediate_geometries": mid, "final_geometries": fin,
        "sql_collapsed": collapse_sql(r.get("sql")), "sql_chars_raw": len(r.get("sql") or ""),
        "timings": r.get("timings"), "llm_calls": [{"purpose": c.get("purpose"), "seconds": c.get("seconds"), "error": c.get("error")}
                                                   for c in r.get("llm_calls", [])],
        "elapsed_seconds": r.get("elapsed_seconds"), "flags": compute_flags(r),
        "notes": r.get("notes"), "plan": r.get("plan"), "geometry_mode": r.get("geometry_mode"),
        "geometry_note": r.get("geometry_note"), "tolerance_m": r.get("tolerance"),
    }


def _gs(s: Optional[dict]) -> str:
    if not s:
        return "-"
    bits = [f"{s.get('type')}", f"{s.get('parts')} part(s)", f"{s.get('points')} pts"]
    if "area_m2" in s:
        bits.append(f"{s['area_m2']} m²")
    if "length_m" in s:
        bits.append(f"{s['length_m']} m")
    if s.get("bbox_m"):
        bits.append(f"extent {s['bbox_m'][0]}x{s['bbox_m'][1]} m")
    return ", ".join(bits)


def render_summary_md(digests: list[dict], info: dict, args) -> str:
    L: list[str] = []
    A = L.append
    A("# geo-mcp revision — summary for review\n")
    A(f"- Run: {info.get('started')} · prompts: {len(digests)} · table `{args.table}` · autonomy level {args.autonomy_level}")
    llm = info.get("llm") or {}
    A(f"- **LLM for the whole run:** backend `{llm.get('backend')}` = `{llm.get('configured_model')}`; model that answered the preflight: "
      f"`{llm.get('served_model')}` (verified: {'yes' if llm.get('served_model') == llm.get('configured_model') else '**NO**'}); "
      f"container env GEO_MCP_LLM_BACKEND was `{llm.get('container_env_backend')}` (ignored for this run)")
    A(f"- Other LLMs resident before / after the run: `{llm.get('other_llms_resident')}` / `{llm.get('other_llms_resident_after')}`; "
      f"embedding model `{llm.get('embedding_model')}`")
    A("- Pipeline (round 2): the LLM extraction is repaired deterministically (app/geo/parsing.py); a bare place name is SELF (the object itself); "
      "BETWEEN/FROM_TO route between the AXES of the anchors (main roads are cheaper), scoped to the main street when there is one; "
      "addresses are matched on the house number; the final geometry is urbis.geo_plan(plan), the same function the SQL calls. "
      "`notes` lists every repair/assumption per prompt.")
    A("- Geometry is NOT included in full: each geometry is described by type/parts/points/area/extent; `summary.json` additionally has a "
      f"coarse outline sketch (polygons -> lines, ≤1 vertex per {args.sketch_spacing:g} m). SQL is shown with every WKT literal collapsed.\n")
    A("## How to review this file in Claude\n")
    A("> For every prompt compare **Expected** (if given) with what happened: LLM detection/extraction, what each entity was resolved to "
      "(candidates, scores, geometry size), the relationship and final geometry, and the automatic flags. Tell me which problems are "
      "(a) LLM extraction, (b) entity resolution/gazetteer, (c) geometry construction (builder/mapviz), (d) missing data. "
      "Group by root cause, say which fix would resolve the most prompts, and name the exact function to change.\n")

    flag_ids: dict[str, list[str]] = {}
    for d in digests:
        for f in d["flags"]:
            flag_ids.setdefault(f["code"], []).append(str(d["id"]))
    A("## Overview\n")
    A("| id | prompt | detect → extract | relationship | status | flags | s |")
    A("|---|---|---|---|---|---|---|")
    for d in digests:
        dr = (d.get("detect") or {}).get("relationship")
        er = (d.get("extraction") or {}).get("relationship") if d.get("extraction") else "-"
        A(f"| {d['id']} | {d['prompt'].strip()[:60].replace('|', '/')} | {dr} → {er} | {d['relationship']} | {d['status']} | "
          f"{', '.join(sorted({f['code'] for f in d['flags']})) or '-'} | {d['elapsed_seconds']} |")
    A("\n### Flags across the batch\n")
    if flag_ids:
        A("| flag | count | prompt ids |")
        A("|---|---|---|")
        for k, v in sorted(flag_ids.items(), key=lambda kv: -len(kv[1])):
            A(f"| {k} | {len(v)} | {', '.join(v)} |")
    else:
        A("No flags.")
    A("")

    A("## Per prompt\n")
    for d in digests:
        A(f"### {d['id']} · {d['prompt'].strip()}  — `{d['status']}`\n")
        if d.get("expected"):
            A(f"- **Expected:** {d['expected']}")
        det = d.get("detect") or {}
        ex = d.get("extraction")
        A(f"- **LLM** (`{(d.get('llm') or {}).get('model')}`): detect → has_spatial={det.get('has_spatial')}, relationship={det.get('relationship')}"
          + (f"; extraction → relationship={ex['relationship']}, distance={ex['distance_meters']}, entities: "
             + "; ".join(f"“{e['text']}” {e['type']}/{e['role']}" for e in ex["entities"]) if ex else ""))
        A(f"- **Pipeline:** relationship `{d['relationship']}`, distance {d['distance_meters']}"
          f"{' (default)' if d['used_default_distance'] else ''}, needs_confirmation={d['needs_confirmation']}"
          + (f", ERROR `{d['error']}`" if d.get("error") else ""))
        for e in d["entities"]:
            A(f"- **“{e['text']}”** ({e['type']}, {e['role']}) — {e['n_candidates']} candidate(s):")
            if e["candidates"]:
                A("  | | id | name | type | score | geometry |")
                A("  |---|---|---|---|---|---|")
                for c in e["candidates"]:
                    A(f"  | {'●' if c['chosen'] else '○'} | {c['id']} | {c['name']} | {c['type']} | {c['score']} | {_gs(c['geometry'])} |")
        for g in d["intermediate_geometries"]:
            A(f"- intermediate: {g['label']} — {_gs(g['stats'])}")
        for g in d["final_geometries"]:
            A(f"- **final:** {g['label']} — {_gs(g['stats'])}")
        if not d["final_geometries"]:
            A("- **final:** none")
        if d["sql_collapsed"]:
            A(f"- SQL ({d['sql_chars_raw']} chars raw): `{d['sql_collapsed']}`")
        t = d.get("timings") or {}
        A(f"- time {d['elapsed_seconds']} s (detect {t.get('detect_s')}, extract {t.get('extract_s')}, resolve {t.get('resolve_s')}, "
          f"geometry {t.get('geometry_s')}, sql {t.get('sql_s')})")
        if d["flags"]:
            A("- **Flags:**")
            for f in d["flags"]:
                A(f"  - `{f['code']}` {f['detail']}")
        A("")
    return "\n".join(L) + "\n"


def read_expected_csv(path: Optional[Path], delimiter: str = ";") -> dict[str, str]:
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh, delimiter=delimiter)
        if not rd.fieldnames or "id" not in rd.fieldnames or "expected" not in rd.fieldnames:
            raise ValueError(f"{path} needs a header 'id;expected'; found {rd.fieldnames}")
        return {str(r["id"]).strip(): (r["expected"] or "").strip() for r in rd if r.get("id")}


def resident_llms(base_url: str, model: str) -> Optional[list[str]]:
    try:
        ps = requests.get(f"{base_url}/api/ps", timeout=10).json().get("models", [])
    except Exception:  # noqa: BLE001
        return None
    emb = {"bge-m3", "all-minilm", "mxbai-embed-large", "nomic-embed-text", "snowflake-arctic-embed"}
    return [n for n in ((m.get("model") or m.get("name")) for m in ps) if n != model and (n or "").split(":")[0] not in emb]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def read_prompts_csv(path: Path, delimiter: str = ";") -> list[dict]:
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")          # tolerates a BOM from Excel
    except UnicodeDecodeError:
        print(f"WARNING: {path} is not UTF-8; reading it as cp1252. Convert: iconv -f CP1252 -t UTF-8", file=sys.stderr)
        text = raw.decode("cp1252")
    reader = csv.DictReader(text.splitlines(), delimiter=delimiter)
    if reader.fieldnames is None or "id" not in reader.fieldnames or "prompt" not in reader.fieldnames:
        raise ValueError(
            f"{path} must have a header row with columns 'id' and 'prompt' "
            f"(delimiter {delimiter!r}); found columns: {reader.fieldnames}"
        )
    return [{"id": row["id"].strip(), "prompt": (row["prompt"] or "").strip()}
            for row in reader if row.get("id") and (row.get("prompt") or "").strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", type=Path, help="CSV file with columns id;prompt")
    parser.add_argument("--outdir", type=Path, default=Path("/srv/resources/urbis/revision"),
                         help="Where to write <id>.json / <id>.html / index.html "
                              "(default reuses the already-mounted /srv/resources/urbis volume)")
    parser.add_argument("--delimiter", default=";")
    parser.add_argument("--table", default="potholes")
    parser.add_argument("--geom-column", default="geom")
    parser.add_argument("--srid", type=int, default=SRID)
    parser.add_argument("--project", default=None)
    parser.add_argument("--autonomy-level", type=int, default=DEFAULT_AUTONOMY_LEVEL, choices=[1, 2, 3])
    parser.add_argument("--buffer-meters", type=float, default=None)
    parser.add_argument("--surface-source", default="urbis.streetsurfaces")
    parser.add_argument("--surface-geom-column", default="geometry")
    parser.add_argument("--llm-backend", default=DEFAULT_LLM_BACKEND,
                        help=f"LLM backend for the WHOLE run (default {DEFAULT_LLM_BACKEND!r}); overrides GEO_MCP_LLM_BACKEND")
    parser.add_argument("--expect-model", default=DEFAULT_EXPECT_MODEL,
                        help=f"abort unless this model answers (default {DEFAULT_EXPECT_MODEL!r}; pass '' to skip the check)")
    parser.add_argument("--expected", type=Path, default=None,
                        help="optional CSV 'id;expected' with what you expect per prompt; shown next to the result in summary.md")
    parser.add_argument("--sketch-spacing", type=float, default=SKETCH_SPACING_M,
                        help="min vertex spacing (m) of the coarse outline sketches in summary.json")
    args = parser.parse_args()

    prompts = read_prompts_csv(args.csv_path, args.delimiter)
    expected = read_expected_csv(args.expected, args.delimiter)
    args.outdir.mkdir(parents=True, exist_ok=True)

    print(f"Loaded {len(prompts)} prompt(s) from {args.csv_path}"
          + (f" and {len(expected)} expectation(s) from {args.expected}" if args.expected else ""))
    llm_info = select_and_verify_llm(args.llm_backend, args.expect_model)
    install_llm_trace()
    try:
        _llm.embed("warm up")                      # load the embedding model before timing
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: embedding warm-up failed: {exc}")
    print(f"LLM: backend={llm_info['backend']} model={llm_info['configured_model']} served_by={llm_info['served_model']} "
          f"(preload {llm_info['preflight_load_s']}s) container_env={llm_info['container_env_backend']!r}")
    if llm_info.get("other_llms_resident"):
        print(f"WARNING: other LLMs are loaded in Ollama: {llm_info['other_llms_resident']} (may evict the model / slow the run)")
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for i, row in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] id={row['id']!r} prompt={row['prompt']!r}")
        record = run_traced_pipeline(
            row["id"], row["prompt"],
            table=args.table, geom_column=args.geom_column, srid=args.srid,
            project=args.project, autonomy_level=args.autonomy_level,
            buffer_meters=args.buffer_meters,
            surface_source=args.surface_source, surface_geom_column=args.surface_geom_column,
        )
        records.append(record)

        json_path = args.outdir / f"{row['id']}.json"
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, default=str, ensure_ascii=False)

        html_path = args.outdir / f"{row['id']}.html"
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(render_html(record))

        status = "ERROR: " + record["error"] if record.get("error") else record.get("relationship")
        print(f"    -> {status} ({record['elapsed_seconds']}s)")

    llm_info["other_llms_resident_after"] = resident_llms(llm_info["base_url"], llm_info["configured_model"])
    digests = [digest_record(r, expected.get(str(r["id"])), args.sketch_spacing) for r in records]
    flags_by_id = {str(d["id"]): sorted({f["code"] for f in d["flags"]}) for d in digests}
    with open(args.outdir / "summary.json", "w", encoding="utf-8") as fh:
        json.dump({"started": started_at, "llm": llm_info, "table": args.table, "prompts": digests},
                  fh, indent=1, default=str, ensure_ascii=False)
    with open(args.outdir / "summary.md", "w", encoding="utf-8") as fh:
        fh.write(render_summary_md(digests, {"started": started_at, "llm": llm_info}, args))

    index_path = args.outdir / "index.html"
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(render_index(records, flags_by_id))

    print(f"\nDone. Wrote {len(records)} report(s) to {args.outdir}")
    print(f"Open {index_path} to browse them.")
    print(f"For Claude: {args.outdir / 'summary.md'} (compact; summary.json adds outline sketches)")
    if llm_info.get("other_llms_resident_after"):
        print(f"WARNING: other LLMs were resident after the run: {llm_info['other_llms_resident_after']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
