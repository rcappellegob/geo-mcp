"""
app/tools/geo_tools.py
=======================
Orchestrates the full geo pipeline: detect -> extract entities -> resolve
each entity to geometry (with disambiguation if ambiguous) -> build SQL.

Autonomy levels (app.config.LEVEL_1_CONFIRM_ALL / LEVEL_2_CONFIRM_RISKY /
LEVEL_3_AUTONOMOUS) control whether geo_pipeline returns a ready-to-run SQL
statement directly, or a "needs_confirmation" response the caller should
surface to the end user before running anything:
  LEVEL_1: always ask, even for a single unambiguous high-confidence match.
  LEVEL_2: ask only when something is "risky" -- an ambiguous match, a
           low-confidence match, or a distance assumed from defaults rather
           than stated explicitly in the prompt.
  LEVEL_3: never ask; always returns "status": "ok" with the best-effort SQL.

Every call is written to the audit log (app.logging_setup.audit) regardless
of outcome, for later threshold/prompt tuning.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.config import (
    DISAMBIGUATION_MARGIN,
    LEVEL_1_CONFIRM_ALL,
    LEVEL_2_CONFIRM_RISKY,
    DEFAULT_AUTONOMY_LEVEL,
    SRID,
)
from app.db import query
from app.geo.builder import AREA_TYPES, build_plan, build_sql, kind_of
from app.geo.detector import detect, extract_entities
from app.geo.resolver import resolve, resolve_address
from app.logging_setup import audit

LOW_CONFIDENCE_SCORE = 0.5
GATE_OVERRIDE_MIN_SCORE = 0.8   # a 1-4 word prompt that matches a gazetteer name this well IS a place


def _short_name_probe(user_query: str) -> Optional[dict]:
    """
    "Edmont machtens" alone was rejected as 'no spatial context' by the intent LLM (item 19). A very short prompt
    that is NOT a question and matches a gazetteer name almost perfectly is a place name: build the extraction
    directly. Anything with a '?' or more than 4 words never takes this path.
    """
    q = (user_query or "").strip()
    if not q or "?" in q or len(q.split()) > 4:
        return None
    try:
        cands = resolve(q, entity_type="ANY")
    except Exception:  # noqa: BLE001
        return None
    if cands and cands[0]["score"] >= GATE_OVERRIDE_MIN_SCORE:
        return {"entities": [{"text": q, "type": "ANY", "role": "anchor"}], "relationship": "SELF", "distance_meters": None,
                "modifier": None, "notes": [f"intent gate said 'no spatial context', but '{q}' matches '{cands[0].get('name_fr')}' "
                                            f"({cands[0]['score']:.2f}): treated as a place name"]}
    return None


def resolve_entity(e: dict, project: Optional[str] = None) -> tuple[list[dict], list[str]]:
    """Candidates (best first) + notes for one extracted entity."""
    notes: list[str] = []
    etype = e.get("type", "ANY")
    # project-extra sources introduce entity types the detector vocabulary does not know -> search all types
    resolve_type = "ANY" if project else etype

    addr = e.get("address")
    if etype == "ADDRESS" and addr:
        cands = resolve_address(addr["street"], addr["number"], addr.get("municipality"))
        if not cands:
            notes.append(f"no address '{addr['street']} {addr['number']}' found: using the street instead")
            cands = resolve(addr["street"], entity_type="STREET", project=project, hint=e.get("hint"))
            for c in cands:
                c["address_fallback"] = True
        elif not cands[0].get("number_exact"):
            notes.append(f"house number {addr['number']} does not exist on {cands[0]['name_fr']}: nearest number on the same side used "
                         f"({cands[0].get('matched_number')})")
        return cands, notes
    if etype == "ADDRESS":
        resolve_type = "STREET"
    return resolve(e["text"], entity_type=resolve_type, project=project, hint=e.get("hint")), notes


def plan_from_extraction(
    user_query: str,
    table: str,
    extraction: dict,
    geom_column: str = "geom",
    srid: int = SRID,
    project: Optional[str] = None,
    autonomy_level: int = DEFAULT_AUTONOMY_LEVEL,
    buffer_meters: Optional[float] = None,
    on_entity: Optional[Callable[[dict], None]] = None,
) -> dict:
    """
    Everything after the LLM: resolve entities, assign roles, build the plan + SQL, decide on confirmation.
    Shared by geo_pipeline and tools/revision.py so the revision report shows exactly what production does.
    `on_entity(record)` lets the report enrich each entity record (timings, GeoJSON) while it is created.
    """
    relationship = (extraction.get("relationship") or "SELF").upper()
    stated_distance = extraction.get("distance_meters")
    modifier = extraction.get("modifier")
    notes = list(extraction.get("notes") or [])

    records: list[dict] = []
    resolved: list[dict] = []
    unresolved: list[str] = []
    ambiguous: list[dict] = []
    low_confidence = False

    for e in extraction.get("entities", []):
        candidates, enotes = resolve_entity(e, project)
        notes.extend(enotes)
        chosen = candidates[0] if candidates else None
        rec = {**e, "match": chosen, "candidates": candidates, "chosen": chosen}
        if on_entity:
            on_entity(rec)
        records.append(rec)
        if not chosen:
            unresolved.append(e["text"])
            continue
        resolved.append({"role": e.get("role", "anchor"), "match": chosen})
        if chosen["score"] < LOW_CONFIDENCE_SCORE:
            low_confidence = True
        if chosen.get("type") == "ADDRESS" and not chosen.get("number_exact", True):
            low_confidence = True
        if len(candidates) > 1 and candidates[1]["score"] >= chosen["score"] * DISAMBIGUATION_MARGIN:
            ambiguous.append({"entity": e["text"], "candidates": candidates})

    base = {
        "relationship": relationship, "entities": records, "ambiguous_entities": ambiguous,
        "autonomy_level": autonomy_level, "notes": notes,
    }
    if unresolved:
        return {**base, "status": "unresolved_entity", "sql": None, "unresolved_entities": unresolved,
                "needs_confirmation": True, "distance_meters": stated_distance, "used_default_distance": False,
                "plan": None, "tolerance": 0}

    stated = stated_distance if stated_distance is not None else buffer_meters
    built = build_plan(relationship, resolved, stated_distance=stated, modifier=modifier)
    notes.extend(built["notes"])
    sql = build_sql(table, built["plan"], built["tolerance"], geom_column=geom_column, srid=srid)

    used_default = built["buffer_source"] in ("default", "default_linear", "default_point")
    risky = bool(ambiguous) or low_confidence or (used_default and built["buffer_source"] == "default")
    needs_confirmation = (
        autonomy_level == LEVEL_1_CONFIRM_ALL
        or (autonomy_level == LEVEL_2_CONFIRM_RISKY and risky)
    )
    return {
        **base, "relationship": built["relationship"], "status": "needs_confirmation" if needs_confirmation else "ok",
        "sql": sql, "distance_meters": built["buffer"], "used_default_distance": used_default,
        "needs_confirmation": needs_confirmation, "plan": built["plan"], "tolerance": built["tolerance"],
    }


def geo_pipeline(
    user_query: str,
    table: str,
    geom_column: str = "geom",
    srid: int = SRID,
    project: Optional[str] = None,
    autonomy_level: int = DEFAULT_AUTONOMY_LEVEL,
    buffer_meters: Optional[float] = None,
    llm_backend: Optional[str] = None,
) -> dict:
    intent = detect(user_query, backend=llm_backend)
    extraction = None
    if not intent.get("has_spatial"):
        extraction = _short_name_probe(user_query)
        if extraction is None:
            result = {
                "status": "ok",
                "sql": f"SELECT * FROM {table}",
                "relationship": "NONE",
                "entities": [],
                "ambiguous_entities": [],
                "needs_confirmation": False,
            }
            audit({"user_query": user_query, "table": table, "project": project, **result})
            return result
    if extraction is None:
        extraction = extract_entities(user_query, backend=llm_backend)

    result = plan_from_extraction(
        user_query, table, extraction, geom_column=geom_column, srid=srid, project=project,
        autonomy_level=autonomy_level, buffer_meters=buffer_meters,
    )
    audit({"user_query": user_query, "table": table, "project": project, **result})
    return result


def resolve_with_choice(
    table: str,
    relationship: str,
    gazetteer_ids: list[str],
    geom_column: str = "geom",
    srid: int = SRID,
    buffer_meters: Optional[float] = None,
) -> dict:
    """
    Second call in the disambiguation loop: the caller already asked the
    user "did you mean X or Y?" (using the `candidates` from a prior
    geo_query response) and now supplies the chosen gazetteer id(s) in
    order. Skips detection/resolution entirely and builds SQL directly.
    """
    if not gazetteer_ids:
        raise ValueError("gazetteer_ids must contain at least one id")

    # Two source tables now: urbis.gazetteer (named places) and
    # urbis.road_class_gazetteer (road-classification matches from
    # resolve_road_class() -- ids prefixed "HIERARCHY:"/"ADMIN:", see
    # sql/lookup_views.sql section 3b). A disambiguation choice can be
    # either kind, so both need checking rather than only urbis.gazetteer.
    rows = query(
        "SELECT id, type, name_fr, name_nl, ST_AsText(geom) AS wkt FROM urbis.gazetteer WHERE id = ANY(%s)"
        " UNION ALL "
        "SELECT id, type, name_fr, name_nl, ST_AsText(geom) AS wkt FROM urbis.road_class_gazetteer WHERE id = ANY(%s)",
        [gazetteer_ids, gazetteer_ids],
    )
    by_id = {r["id"]: r for r in rows}
    for gid in gazetteer_ids:  # an ADDRESS candidate chosen by the user: "ADDRESS:<street_id>:<number>"
        if gid.startswith("ADDRESS:") and gid not in by_id:
            _, sid, num = gid.split(":", 2)
            arows = query(
                """SELECT ST_AsText(ST_Centroid(ST_Collect(a.geometry))) AS wkt, MIN(a.street_name_fr) AS name_fr, MIN(a.street_name_nl) AS name_nl,
                          (SELECT g.gid FROM urbis.street_groups g WHERE a.street_id = ANY(g.pids) LIMIT 1) AS street_gid
                   FROM urbis.mv_addresses_lookup a WHERE a.street_id = %s AND a.policenum = %s GROUP BY a.street_id""",
                [int(sid), num],
            )
            if arows:
                by_id[gid] = {"id": gid, "type": "ADDRESS", "name_fr": f"{arows[0]['name_fr']} {num}",
                              "name_nl": f"{arows[0]['name_nl']} {num}", "wkt": arows[0]["wkt"], "street_gid": arows[0]["street_gid"]}
    missing = [gid for gid in gazetteer_ids if gid not in by_id]
    if missing:
        raise ValueError(f"Unknown gazetteer id(s): {missing}")

    rel = (relationship or "SELF").upper()
    resolved = []
    for i, gid in enumerate(gazetteer_ids):
        role = "anchor"
        if rel in ("BETWEEN", "FROM_TO"):
            role = "start" if i == 0 else ("end" if i == len(gazetteer_ids) - 1 else "via")
        resolved.append({"role": role, "match": {"id": gid, "type": by_id[gid]["type"], "wkt": by_id[gid].get("wkt"), "street_gid": by_id[gid].get("street_gid")}})
    built = build_plan(rel, resolved, stated_distance=buffer_meters)
    sql = build_sql(table, built["plan"], built["tolerance"], geom_column=geom_column, srid=srid)
    result = {
        "status": "ok", "sql": sql, "relationship": built["relationship"], "gazetteer_ids": gazetteer_ids, "plan": built["plan"],
        # Mirrors geo_query's entities shape (text/match.id/match.type/
        # match.wkt) so callers -- specifically chat-platform's
        # result_summarizer.py/build_geometry_feature_collection -- can
        # treat a resolve_with_choice result exactly like a geo_query
        # result for map display, with no special-casing needed. The WKTs
        # were already fetched above (by_id) to build the SQL; this just
        # exposes them instead of discarding them.
        "entities": [
            {
                "text": gid,
                "match": {
                    "id": gid, "type": by_id[gid]["type"],
                    "name_fr": by_id[gid]["name_fr"], "name_nl": by_id[gid]["name_nl"],
                    "wkt": by_id[gid]["wkt"],
                },
            }
            for gid in gazetteer_ids
        ],
    }
    audit({"table": table, "resolve_with_choice": True, **result})
    return result
