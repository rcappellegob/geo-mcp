"""
app/tools/query_geometry.py
===========================
The geometry a geo_query question actually SELECTS, as simplified EPSG:4326 GeoJSON, for map display and
for the workshop prize. geo_query itself only returns SQL (the geometry is computed when that SQL runs),
so this mirrors build_where's relationship logic -- same helpers as app/tools/revision.py's report maps:

  NEAR / ON      -> metric buffer around the first anchor
  WITHIN         -> the anchors themselves (areas / lines)
  INTERSECTION   -> buffered intersection of the first two anchors
  BETWEEN/FROM_TO-> the real route corridor (street surfaces between the anchors), else the buffered line
  anything else  -> NEAR buffer of the first anchor (build_where's last line)

Additive: a new file + one registration in main.py; nothing existing is changed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from app.config import DEFAULT_BUFFER_METERS, ON_BUFFER_METERS, SRID
from app.db import query

# The mirrored (older) logic needs helpers that newer geo-mcp versions may no longer have: import them
# lazily so the server always starts, and the exact urbis.geo_target() path below keeps working.
try:  # pragma: no cover
    from app.geo import mapviz as _mapviz
except Exception:  # noqa: BLE001
    _mapviz = None


def _helper(name):
    fn = getattr(_mapviz, name, None) if _mapviz else None
    if fn is None:
        raise RuntimeError(f"app.geo.mapviz.{name} not available in this geo-mcp version")
    return fn

log = logging.getLogger("geo_mcp.query_geometry")

SIMPLIFY_DEGREES = 0.00001  # ~1 m: keeps the payload small for polling pages


def _simplify(gj: Optional[dict]) -> Optional[dict]:
    if not gj:
        return None
    rows = query(
        "SELECT ST_AsGeoJSON(ST_SimplifyPreserveTopology(ST_GeomFromGeoJSON(%s), %s), 6) AS gj",
        [json.dumps(gj), SIMPLIFY_DEGREES],
    )
    return json.loads(rows[0]["gj"]) if rows and rows[0]["gj"] else gj


_GEO_TARGET = re.compile(r"urbis\.geo_target\(\s*'((?:[^']|'')*)'\s*::\s*jsonb\s*\)", re.S)


def _from_geo_target(sql: str, srid: int) -> Optional[dict]:
    """
    Newer pipeline: build_sql() delegates the target geometry to the SQL function
    urbis.geo_target('<json>'::jsonb). Run THAT SAME function on the JSON found in the geo_query SQL, so the
    map shows exactly what the real query selects (no second implementation to keep in sync). The JSON
    is passed as a bound parameter -- the SQL text itself is never executed.
    """
    m = _GEO_TARGET.search(sql or "")
    if not m:
        return None
    payload = m.group(1).replace("''", "'")
    try:
        json.loads(payload)
    except ValueError:
        return None
    rows = query(
        "SELECT ST_AsGeoJSON(ST_Transform(ST_SetSRID(urbis.geo_target(%s::jsonb), %s), 4326)) AS gj",
        [payload, srid],
    )
    gj = json.loads(rows[0]["gj"]) if rows and rows[0]["gj"] else None
    return {"geojson": _simplify(gj), "label": "query geometry (urbis.geo_target)"} if gj else None


def query_geometry_geojson(
    relationship: str, wkts: list[str], srid: int = SRID, buffer_meters: float = 0, sql: str = "",
) -> dict:
    exact_error = None
    if sql:
        t0 = time.time()
        try:
            exact = _from_geo_target(sql, srid)
        except Exception as exc:  # noqa: BLE001 -- fall back to the mirrored logic below, but say why
            exact = None
            log.exception("urbis.geo_target failed after %.1fs", time.time() - t0)
            exact_error = f"{type(exc).__name__}: {str(exc).strip()[:300]}"
        else:
            exact_error = None if exact else "no urbis.geo_target(...) call found in the sql, or it returned NULL"
            log.info("urbis.geo_target %s in %.1fs", "ok" if exact else "gave nothing", time.time() - t0)
        if exact:
            return exact
    try:
        return _mirrored(relationship, wkts, srid, buffer_meters)
    except Exception as exc:  # noqa: BLE001 -- the caller (workshop backend) logs this message
        raise RuntimeError(f"query_geometry failed. urbis.geo_target: {exact_error or 'not tried (no sql given)'}; "
                           f"fallback: {type(exc).__name__}: {exc}") from exc


def _mirrored(relationship: str, wkts: list[str], srid: int, buffer_meters: float) -> dict:
    wkts = [w for w in wkts if w]
    if not wkts:
        return {"geojson": None, "label": "no geometry"}
    buf = buffer_meters or None
    rel = (relationship or "").upper()

    if rel in ("NEAR", "ON"):
        b = buf if buf is not None else (DEFAULT_BUFFER_METERS if rel == "NEAR" else ON_BUFFER_METERS)
        return {"geojson": _simplify(_helper('buffer_geojson')(wkts[0], b, srid)), "label": f"{rel} buffer ({b} m)"}

    if rel == "WITHIN":
        geoms = [g for g in _helper('wkts_to_geojson')(wkts, srid) if g]
        gj = {"type": "GeometryCollection", "geometries": geoms} if len(geoms) > 1 else (geoms[0] if geoms else None)
        return {"geojson": gj, "label": "containment area"}

    if rel == "INTERSECTION" and len(wkts) >= 2:
        b = buf if buf is not None else DEFAULT_BUFFER_METERS
        return {"geojson": _simplify(_helper('intersection_buffer_geojson')(wkts[0], wkts[1], b, srid)),
                "label": f"intersection buffer ({b} m)"}

    if rel in ("BETWEEN", "FROM_TO") and len(wkts) >= 2:
        corridor_gj, final_gj = _helper('between_geojson')(wkts, srid)
        if final_gj:
            return {"geojson": _simplify(final_gj), "label": "street surfaces between the anchors"}
        return {"geojson": _simplify(corridor_gj), "label": "corridor between the anchors (fallback)"}

    b = buf if buf is not None else DEFAULT_BUFFER_METERS
    return {"geojson": _simplify(_helper('buffer_geojson')(wkts[0], b, srid)), "label": f"fallback NEAR buffer ({b} m)"}
