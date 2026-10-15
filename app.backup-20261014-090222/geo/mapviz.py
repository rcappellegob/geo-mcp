"""
app/geo/mapviz.py
==================
Small helpers that turn EPSG:3812 WKT geometries into EPSG:4326 GeoJSON
(what Leaflet expects), via PostGIS ST_Transform. Used by app/tools/revision.py
to build the reasoning maps; kept separate from resolver.py/builder.py since
those return WKT in the working SRID on purpose (map display is a separate
concern from the SQL-building pipeline).
"""

from __future__ import annotations

import json
from typing import Optional

from app.config import SRID
from app.db import query
from app.geo.builder import _validate_identifier


def wkts_to_geojson(wkts: list[str], srid: int = SRID) -> list[Optional[dict]]:
    """Batch-transform a list of WKT strings to GeoJSON geometries in EPSG:4326."""
    wkts = [w for w in wkts if w]
    if not wkts:
        return []
    rows = query(
        "SELECT ST_AsGeoJSON(ST_Transform(ST_GeomFromText(w, %s), 4326)) AS gj "
        "FROM unnest(%s::text[]) AS w",
        [srid, wkts],
    )
    return [json.loads(r["gj"]) if r["gj"] else None for r in rows]


def buffer_geojson(wkt: str, buffer_meters: float, srid: int = SRID) -> Optional[dict]:
    """GeoJSON (EPSG:4326) of a metric buffer around a WKT geometry, built in the projected SRID."""
    rows = query(
        "SELECT ST_AsGeoJSON(ST_Transform(ST_Buffer(ST_GeomFromText(%s, %s), %s), 4326)) AS gj",
        [wkt, srid, buffer_meters],
    )
    return json.loads(rows[0]["gj"]) if rows and rows[0]["gj"] else None


def intersection_buffer_geojson(wkt_a: str, wkt_b: str, buffer_meters: float, srid: int = SRID) -> Optional[dict]:
    """GeoJSON (EPSG:4326) of a buffered intersection point/area of two WKT geometries."""
    rows = query(
        "SELECT ST_AsGeoJSON(ST_Transform(ST_Buffer("
        "ST_Intersection(ST_GeomFromText(%s, %s), ST_GeomFromText(%s, %s)), %s), 4326)) AS gj",
        [wkt_a, srid, wkt_b, srid, buffer_meters],
    )
    return json.loads(rows[0]["gj"]) if rows and rows[0]["gj"] else None


def plan_geometry(plan: dict) -> dict:
    """
    Evaluate a builder plan exactly as the generated SQL does (urbis.geo_plan) and return GeoJSON (EPSG:4326):
        {"target": <geometry the table is filtered with>, "path": <route axes or None>, "mode": str, "note": str|None}
    """
    rows = query(
        "SELECT ST_AsGeoJSON(ST_Transform(target, 4326)) AS target, ST_AsGeoJSON(ST_Transform(path, 4326)) AS path, mode, note "
        "FROM urbis.geo_plan(%s::jsonb)",
        [json.dumps(plan)],
    )
    if not rows:
        return {"target": None, "path": None, "mode": None, "note": None}
    r = rows[0]
    return {
        "target": json.loads(r["target"]) if r["target"] else None,
        "path": json.loads(r["path"]) if r["path"] else None,
        "mode": r["mode"], "note": r["note"],
    }
