"""
app/geo/builder.py
===================
Step 3 of the pipeline: resolved entities + relationship -> (a) a small JSON *plan* and (b) the SQL for the
caller's table.

Revision round 2 moved all geometry work into the database (sql/geo_functions.sql: urbis.geo_plan /
urbis.geo_target), so this module no longer embeds WKT literals or a routing CTE in the SQL. The statement
handed back is simply

    SELECT t.* FROM <table> t
    WHERE ST_DWithin(t.geom, (SELECT urbis.geo_target('<plan>'::jsonb)), <tolerance>)

and the report / map call the very same function, so what you see is what is queried.

Semantics decided with the user (rounds 1-2):
  SELF / ON / WITHIN : the object itself. A street = its own segments (+ junctions between them); a zone =
                       its polygon, NO buffer; a bare POI/stop/address = small buffer; a line = 20 m.
  NEAR               : buffer = stated distance, else 50 m (point / street) or 20 m (linear objects).
  BETWEEN / FROM_TO  : "X between Y and Z" -> the stretch of X between its crossings with Y and Z;
                       otherwise the cheapest road between the AXES of the anchors (main roads cost less).
                       An address on street S plus another street, or two addresses on one street, imply
                       main = S. A railway between two points is buffered 20 m.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.config import (
    AREA_BUFFER_METERS,
    DEFAULT_BUFFER_METERS,
    LINEAR_BUFFER_METERS,
    ROAD_TOLERANCE_METERS,
    ROUTE_HALF_WIDTH_METERS,
    SELF_POINT_BUFFER_METERS,
    SRID,
)

AREA_TYPES = {"MUNICIPALITY", "STATISTICAL_SECTOR", "MONITORING_DISTRICT", "REGION", "NEIGHBOUR_MUNICIPALITY"}
POINT_TYPES = {"POI", "TRANSIT_STOP", "ADDRESS"}
LINE_TYPES = {"TRANSIT_LINE", "RAILWAY", "OUTSIDE_STREET"}

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _validate_identifier(name: str, what: str) -> str:
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {what} {name!r}: must be a plain (optionally schema-qualified) "
            f"SQL identifier, e.g. 'potholes' or 'app.potholes'."
        )
    return name


def kind_of(etype: str) -> str:
    """AREA | POINT | LINE | STREET (STREET also covers road classes such as 'regional roads')."""
    if etype in AREA_TYPES:
        return "AREA"
    if etype in POINT_TYPES:
        return "POINT"
    if etype in LINE_TYPES:
        return "LINE"
    return "STREET"


def _ref(match: dict, role: str) -> dict:
    if match.get("type") == "ADDRESS":
        return {"wkt": match["wkt"], "type": "ADDRESS", "role": role, "street_gid": match.get("street_gid")}
    return {"gid": match["id"], "type": match["type"], "role": role}


def build_plan(
    relationship: str,
    resolved: list[dict],
    stated_distance: Optional[float] = None,
    modifier: Optional[str] = None,
) -> dict:
    """
    resolved: [{"role": "anchor|main|start|via|end", "match": <chosen candidate dict>}, ...] in prompt order.
    Returns {"plan", "tolerance", "buffer", "buffer_source", "notes"}.
    """
    rel = (relationship or "SELF").upper()
    if rel in ("NONE", ""):
        rel = "SELF"
    notes: list[str] = []
    between = rel in ("BETWEEN", "FROM_TO")

    ents: list[dict] = []
    for r in resolved:
        m = r["match"]
        role = r.get("role") or "anchor"
        if kind_of(m["type"]) == "AREA":
            role = "area"
        elif not between and role in ("start", "end", "via", "main"):
            role = "anchor"
        ents.append(_ref(m, role))

    if between:
        starts = [e for e in ents if e["role"] == "start"]
        ends = [e for e in ents if e["role"] == "end"]
        has_main = any(e["role"] == "main" for e in ents)
        if starts and ends and not has_main:
            main_gid = _infer_main(starts[0], ends[-1])
            if main_gid:
                ents.insert(0, {"gid": main_gid, "type": "STREET", "role": "main"})
                notes.append(f"main street inferred from the address(es): {main_gid}")
        if not (starts and ends):
            # not enough anchors to route: degrade to the object itself instead of silently guessing
            rel = "SELF"
            for e in ents:
                if e["role"] in ("start", "end", "via", "main"):
                    e["role"] = "anchor"
            notes.append("BETWEEN/FROM_TO needs a start and an end; treated as the objects themselves")
            between = False

    primaries = [e for e in ents if e["role"] != "area"]
    kinds = {kind_of(e["type"]) for e in primaries}
    buffer: Optional[float] = None
    source: Optional[str] = None

    if between:
        main = next((e for e in ents if e["role"] == "main"), None)
        if main is not None and main["type"] == "RAILWAY":
            buffer = stated_distance if stated_distance is not None else LINEAR_BUFFER_METERS
            source = "stated" if stated_distance is not None else "default_linear"
        tolerance = 0 if buffer else ROAD_TOLERANCE_METERS
    else:
        if stated_distance is not None:
            buffer, source = stated_distance, "stated"
        elif rel in ("NEAR", "INTERSECTION"):
            if not primaries:
                buffer, source = (AREA_BUFFER_METERS or None), None
            elif kinds <= {"LINE"}:
                buffer, source = LINEAR_BUFFER_METERS, "default_linear"
            else:
                buffer, source = DEFAULT_BUFFER_METERS, "default"
        else:  # SELF / ON / WITHIN
            if kinds and kinds <= {"LINE"} and modifier is None:
                buffer, source = LINEAR_BUFFER_METERS, "default_linear"
            elif "POINT" in kinds:
                buffer, source = SELF_POINT_BUFFER_METERS, "default_point"
        if buffer == 0:
            buffer = None
        if buffer:
            tolerance = 0
        elif not primaries or kinds <= {"AREA"}:
            tolerance = 0
        else:
            tolerance = ROAD_TOLERANCE_METERS

    plan = {"rel": rel, "ents": ents, "buffer": buffer, "modifier": modifier, "half_width": ROUTE_HALF_WIDTH_METERS}
    if not modifier:
        plan.pop("modifier")
    return {"plan": plan, "tolerance": tolerance, "buffer": buffer, "buffer_source": source, "notes": notes, "relationship": rel}


def _infer_main(start: dict, end: dict) -> Optional[str]:
    """Two addresses on one street, or an address plus another street -> the route lies ON the address' street."""
    s_gid = start.get("street_gid") if start.get("type") == "ADDRESS" else None
    e_gid = end.get("street_gid") if end.get("type") == "ADDRESS" else None
    if s_gid and e_gid:
        return s_gid if s_gid == e_gid else None
    if s_gid and end.get("type") == "STREET" and end.get("gid") != s_gid:
        return s_gid
    if e_gid and start.get("type") == "STREET" and start.get("gid") != e_gid:
        return e_gid
    return None


def plan_expr(plan: dict) -> str:
    """Scalar sub-select returning the target geometry (evaluated once per statement)."""
    literal = json.dumps(plan, ensure_ascii=False, separators=(",", ":")).replace("'", "''")
    return f"(SELECT urbis.geo_target('{literal}'::jsonb))"


def build_sql(
    table: str,
    plan: dict,
    tolerance: float = 0,
    geom_column: str = "geom",
    srid: int = SRID,
) -> str:
    table = _validate_identifier(table, "table")
    col = f"t.{_validate_identifier(geom_column, 'geom_column')}"
    lhs = col if srid == SRID else f"ST_Transform({col}, {SRID})"
    expr = plan_expr(plan)
    pred = f"ST_DWithin({lhs}, {expr}, {tolerance:g})" if tolerance else f"ST_Intersects({lhs}, {expr})"
    return f"SELECT t.* FROM {table} t\nWHERE {pred}"
