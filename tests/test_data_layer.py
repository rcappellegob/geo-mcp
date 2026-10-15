"""
tests/test_data_layer.py
========================
Integration test of the reference-data layer (STIB stops/lines, rail, OSM POIs with aliases,
roads + municipalities in a 5 km belt) against a REAL PostGIS database. No network is used:
the WFS and Overpass calls are replaced by small fixtures.

Skipped unless a scratch database is given (it creates and DROPS schema "urbis" in it!):

    GEO_MCP_TEST_PG="host=127.0.0.1 port=5432 user=tester password=tester dbname=geo_test" \
        pytest tests/test_data_layer.py -v

The database needs the extensions postgis, unaccent, pg_trgm (vector is optional).
"""
from __future__ import annotations

import os
import re
from argparse import Namespace
from pathlib import Path

import pytest

DSN = os.environ.get("GEO_MCP_TEST_PG")
pytestmark = pytest.mark.skipif(not DSN, reason="set GEO_MCP_TEST_PG to a scratch PostGIS database to run")

APP = Path(__file__).resolve().parents[1] / "app"


def _kv(dsn: str) -> dict:
    return dict(p.split("=", 1) for p in dsn.split())


# --------------------------------------------------------------------------- fixtures
def _wfs_response(features):
    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"type": "FeatureCollection", "features": features}
    return R()


def _feat(props, geom):
    return {"type": "Feature", "properties": props, "geometry": geom}


def _pt(x, y):
    return {"type": "Point", "coordinates": [x, y]}


def _ml(*lines):
    return {"type": "MultiLineString", "coordinates": [list(map(list, ln)) for ln in lines]}


STOPS = [  # gid, line, mode, stop_id, names, x, y   (real "Chien Vert" stop positions from the production DB)
    (334, [36], "Bus", "1816", 654103.9, 669347.5), (361, [36, 206], "Bus", "1859", 654261.7, 669387.9),
    (1558, [206], "Bus", "1546", 654000.0, 669300.0), (1966, [39, 44], "Tram", "5504", 654150.0, 669450.0),
    (1996, [39, 44], "Tram", "5559", 654200.0, 669500.0),
]
WFS = {
    "stib_stops": [_feat({"gid": g, "line": ln, "mode": m, "stop_id": sid, "name_fr": "Chien Vert", "name_nl": "Groene Hond"}, _pt(x, y))
                   for g, ln, m, sid, x, y in STOPS]
                  + [_feat({"gid": 9000, "line": [71], "mode": "Bus", "stop_id": "9999", "name_fr": "Chien Vert", "name_nl": "Groene Hond"}, _pt(660500, 669300)),
                     _feat({"gid": 9001, "line": [71], "mode": "Bus", "stop_id": "7001", "name_fr": "Meiser", "name_nl": "Meiser"}, _pt(654800, 670800))],
    "stib_lines": [
        _feat({"line": "8", "mode": "Tram"}, _ml([(650000, 668000), (651000, 668500)])),
        _feat({"line": 8, "mode": "tram"}, _ml([(651000, 668500), (652000, 669000)])),      # same line, int + lowercase mode
        _feat({"line": "71", "mode": "Bus"}, _ml([(648000, 667000), (649000, 667500)])),
        _feat({"line": "1", "mode": "METRO"}, _ml([(647000, 668000), (648000, 668000)])),
        _feat({"line": "99", "mode": "Funiculaire"}, _ml([(647000, 660000), (648000, 660000)])),  # must be dropped
    ],
    "infrabel_lines": [
        _feat({"linenumber": 50, "id": 1}, _ml([(645000, 670000), (648000, 671000)])),
        _feat({"linenumber": 50, "id": 2}, _ml([(648000, 671000), (651000, 672000)])),
        _feat({"linenumber": 161, "id": 3}, _ml([(650000, 665000), (652000, 666000)])),
    ],
}

OSM_POIS = [
    {"type": "way", "id": 229587235, "center": {"lat": 50.8353, "lon": 4.4165},
     "tags": {"name": "Palais Stoclet", "name:nl": "Stocletpaleis", "name:en": "Stoclet Palace", "historic": "building",
              "wikidata": "Q1", "alt_name": "Stoclet House;Palais Stoclet"}},
    {"type": "node", "id": 1, "lat": 50.8949, "lon": 4.3415,
     "tags": {"name": "Atomium", "name:fr": "Atomium", "name:nl": "Atomium", "tourism": "attraction", "alt_name": "Atom"}},
    {"type": "node", "id": 2, "lat": 50.85, "lon": 4.35, "tags": {"amenity": "bench"}},  # no name: skipped
]


def _way(i, name, coords, **tags):
    return {"type": "way", "id": i, "tags": {"highway": tags.pop("highway", "primary"), "name": name, **tags},
            "geometry": [{"lat": la, "lon": lo} for lo, la in coords]}


OSM_ROADS = [
    _way(1, "Ninoofsesteenweg", [(4.20, 50.84), (4.24, 50.84), (4.30, 50.84)], ref="N8"),   # crosses the border
    _way(2, "Ring", [(4.19, 50.80), (4.19, 50.88)], highway="motorway", ref="R0;E19"),      # outside, by ref
    _way(3, "Rue Inside", [(4.30, 50.80), (4.31, 50.82)]),                                  # inside: clipped away
    _way(4, "Very Far Road", [(3.90, 50.80), (3.91, 50.81)]),                               # beyond the belt
    _way(5, "Rue Test", [(4.19, 50.82), (4.20, 50.83)]),                                    # same name as a Region street
]


def _rel(i, name, box, **tags):
    (x0, y0), (x1, y1) = box
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    members = [{"type": "way", "ref": 100 * i + k, "role": "outer",
                "geometry": [{"lat": a[1], "lon": a[0]}, {"lat": b[1], "lon": b[0]}]}
               for k, (a, b) in enumerate(zip(ring, ring[1:]))]
    return {"type": "relation", "id": i, "members": members, "tags": {"name": name, "boundary": "administrative", **tags}}


OSM_BOUNDARIES = [
    _rel(10, "Dilbeek", ((4.15, 50.82), (4.23, 50.88)), **{"ref:NIS": "23009"}),
    _rel(11, "Linkebeek", ((4.30, 50.74), (4.34, 50.78)), **{"ref:NIS": "23100", "name:fr": "Linkebeek", "name:nl": "Linkebeek"}),
    _rel(12, "Ixelles", ((4.30, 50.84), (4.34, 50.86))),   # inside the Region -> must be dropped
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    import geopandas as gpd
    import psycopg2
    from pyproj import Transformer
    from shapely.geometry import box
    from shapely.ops import transform
    from sqlalchemy import create_engine

    import app.config as config
    import app.db
    from app import sync_urbis as su

    kv = _kv(DSN)
    mp = pytest.MonkeyPatch()
    for k, v in (("PGHOST", kv["host"]), ("PGPORT", kv.get("port", "5432")), ("PGUSER", kv["user"]),
                 ("PGPASSWORD", kv.get("password", "")), ("PGDATABASE", kv["dbname"])):
        mp.setattr(config, k, v)
    cfg = su.DbConfig(host=kv["host"], port=int(kv.get("port", 5432)), user=kv["user"], password=kv.get("password"),
                      dbname=kv["dbname"], schema="urbis")

    conn = psycopg2.connect(cfg.dsn())
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS urbis CASCADE; CREATE SCHEMA urbis; SET search_path TO urbis, public;")
    # an OLD-style osm_pois placeholder, as created by the previous version of lookup_views.sql
    cur.execute("CREATE TABLE urbis.osm_pois (osm_id text, name text, category text, geometry geometry(Point, 3812))")

    to3812 = Transformer.from_crs(4326, 3812, always_xy=True).transform
    region = transform(to3812, box(4.24, 50.76, 4.48, 50.92))
    gpd.GeoDataFrame({"namefre": ["Bruxelles"]}, geometry=[region], crs="EPSG:3812").to_postgis(
        "region", create_engine(cfg.sqlalchemy_url()), schema="urbis", if_exists="replace", index=False)

    def fake_get(url, timeout=None, **kw):
        for key, feats in WFS.items():
            if f"bm_public_transport:{key}" in url:
                return _wfs_response(feats)
        raise AssertionError(f"unexpected URL {url}")

    def fake_overpass(ql, **kw):
        if '"highway"' in ql:
            return {"elements": OSM_ROADS}
        if 'boundary' in ql:
            return {"elements": OSM_BOUNDARIES}
        return {"elements": OSM_POIS}

    mp.setattr(su.requests, "get", fake_get)
    mp.setattr(su, "overpass_query", fake_overpass)
    args = Namespace(skip_stib=False, skip_stib_lines=False, skip_rail=False, skip_osm_pois=False,
                     skip_outside=False, outside_buffer_m=5000)
    loaded = su.load_reference_sources(cfg, args)

    # placeholders / column upgrades: the part of lookup_views.sql before section 0b
    sql = (APP / "sql" / "lookup_views.sql").read_text(encoding="utf-8")
    head = sql[sql.index("CREATE TABLE IF NOT EXISTS urbis.stib_stops"): sql.index("-- 0b.")]
    cur.execute(head)

    # reduced gazetteer = one dummy Region street + the REFERENCE BRANCHES sliced out of the real file
    branches = sql[sql.index("-- >>> REFERENCE BRANCHES BEGIN"): sql.index("-- <<< REFERENCE BRANCHES END")]
    cur.execute(f"""
        DROP MATERIALIZED VIEW IF EXISTS urbis.gazetteer CASCADE;
        CREATE MATERIALIZED VIEW urbis.gazetteer AS
        SELECT 'STREET:1'::text AS id, 'STREET'::text AS type, 1::bigint AS source_id, 'placenames'::text AS source_table,
               'Rue Test'::text AS name_fr, 'Teststraat'::text AS name_nl, 'rue test'::text AS name_fr_norm,
               'teststraat'::text AS name_nl_norm,
               ST_GeomFromText('LINESTRING(650000 668000, 650100 668100)', 3812) AS geom,
               ST_GeomFromText('POINT(650000 668000)', 3812) AS centroid
        UNION ALL
        {branches}
        ;
        CREATE UNIQUE INDEX ux_gazetteer_id ON urbis.gazetteer (id);
        CREATE INDEX ON urbis.gazetteer USING gin (name_fr_norm gin_trgm_ops);
        CREATE INDEX ON urbis.gazetteer USING gin (name_nl_norm gin_trgm_ops);
    """)
    yield {"cfg": cfg, "cur": cur, "loaded": loaded, "su": su, "region": region}
    mp.undo()
    conn.close()


def q(db, sql):
    db["cur"].execute(sql)
    return db["cur"].fetchall()


# --------------------------------------------------------------------------- loaders
def test_all_sources_loaded(db):
    assert set(db["loaded"]) == {"stib_stops", "stib_lines", "infrabel_lines", "osm_pois", "outside_roads",
                                 "neighbour_municipalities"}


def test_stib_lines_filtered_and_normalised(db):
    rows = q(db, "SELECT mode, line, count(*) FROM urbis.stib_lines GROUP BY 1,2 ORDER BY 1,2")
    assert rows == [("Bus", "71", 1), ("Metro", "1", 1), ("Tram", "8", 2)]        # Funiculaire dropped, 8.0 -> '8'


def test_rail_columns_detected(db):
    assert q(db, "SELECT DISTINCT line_ref FROM urbis.infrabel_lines ORDER BY 1") == [("161",), ("50",)]


def test_osm_aliases_stored(db):
    row = q(db, "SELECT name, name_nl, name_fr, name_en, alt_name, category, wikidata FROM urbis.osm_pois WHERE osm_id='way/229587235'")[0]
    assert row == ("Palais Stoclet", "Stocletpaleis", None, "Stoclet Palace", "Stoclet House;Palais Stoclet", "building", "Q1")
    assert q(db, "SELECT count(*) FROM urbis.osm_pois")[0][0] == 2                  # unnamed element skipped


def test_outside_roads_clipped_to_belt(db):
    names = {r[0] for r in q(db, "SELECT name FROM urbis.outside_roads")}
    assert names == {"Ninoofsesteenweg", "Ring", "Rue Test"}                         # inside + too-far roads dropped
    region_wkt = db["region"].wkt
    inside = q(db, f"SELECT count(*) FROM urbis.outside_roads WHERE ST_Intersects(geometry, ST_GeomFromText('{region_wkt}', 3812)) "
                   f"AND ST_Length(ST_Intersection(geometry, ST_GeomFromText('{region_wkt}', 3812))) > 1")[0][0]
    assert inside == 0                                                               # nothing left inside the Region


def test_neighbour_municipalities(db):
    rows = q(db, "SELECT name, nis FROM urbis.neighbour_municipalities ORDER BY name")
    assert rows == [("Dilbeek", "23009"), ("Linkebeek", "23100")]                    # Ixelles (inside Region) dropped


def test_placeholder_upgrade_added_alias_columns(db):
    cols = {r[0] for r in q(db, "SELECT column_name FROM information_schema.columns WHERE table_schema='urbis' AND table_name='osm_pois'")}
    assert {"name_nl", "name_fr", "name_en", "alt_name", "wikidata"} <= cols


# --------------------------------------------------------------------------- gazetteer view
def test_gazetteer_rows(db):
    got = dict(q(db, "SELECT type, count(*) FROM urbis.gazetteer GROUP BY 1"))
    assert got["TRANSIT_STOP"] == 3            # Chien Vert (5 stops -> 1), Chien Vert far away, Meiser
    assert got["TRANSIT_LINE"] == 6            # 3 lines x (name + spoken variant)
    assert got["RAILWAY"] == 2 + 3             # 2 lines + 3 generic names
    assert got["NEIGHBOUR_MUNICIPALITY"] == 2
    assert got["OUTSIDE_STREET"] == 3 + 3      # 3 names + refs N8, R0, E19  (R0;E19 split)
    # 2 main rows + 3 alias rows: "Stoclet Palace" (name:en), "Stoclet House" (alt_name), "Atom" (alt_name);
    # the alt_name "Palais Stoclet" equals the main name and must NOT become a duplicate row
    assert got["POI"] == 2 + 3
    ids = [r[0] for r in q(db, "SELECT id FROM urbis.gazetteer")]
    assert len(ids) == len(set(ids))


def test_chien_vert_merged_by_name_and_cluster(db):
    rows = q(db, "SELECT id FROM urbis.gazetteer WHERE type='TRANSIT_STOP' AND name_fr_norm='chien vert' ORDER BY id")
    assert rows == [("TRANSIT_STOP:1546",), ("TRANSIT_STOP:9999",)]


def test_refresh_concurrently_works(db):
    db["cur"].execute("REFRESH MATERIALIZED VIEW CONCURRENTLY urbis.gazetteer")


# --------------------------------------------------------------------------- resolver
def ids(cands):
    return [c["id"] for c in cands]


def test_palais_stoclet_found_by_english_alias_once(db):
    from app.geo.resolver import resolve
    c = resolve("stocklet palace", entity_type="POI")
    assert ids(c).count("OSM_POI:way/229587235") == 1                                # alias rows collapsed
    assert c[0]["id"] == "OSM_POI:way/229587235"


def test_tram_line_by_both_phrasings(db):
    from app.geo.resolver import resolve
    assert resolve("tram 8", entity_type="TRANSIT_LINE")[0]["id"] == "TRANSIT_LINE:tram:8"
    assert resolve("ligne de tram 8", entity_type="TRANSIT_LINE")[0]["id"] == "TRANSIT_LINE:tram:8"


def test_railway_generic_names(db):
    from app.geo.resolver import resolve
    assert resolve("spoorweg", entity_type="RAILWAY")[0]["id"] == "RAILWAY:ALL"
    assert resolve("chemin de fer", entity_type="RAILWAY")[0]["id"] == "RAILWAY:ALL"


def test_context_penalty_region_street_wins_tie(db):
    from app.config import CONTEXT_PENALTY
    from app.geo.resolver import resolve
    c = resolve("rue test", entity_type="STREET")
    assert c[0]["id"] == "STREET:1"
    outside = [x for x in c if x["id"].startswith("OUTSIDE_STREET:")]
    assert outside and c[0]["score"] - outside[0]["score"] >= CONTEXT_PENALTY - 0.02


def test_outside_context_still_findable(db):
    from app.geo.resolver import resolve
    assert resolve("ninoofsesteenweg", entity_type="STREET")[0]["id"].startswith("OUTSIDE_STREET:")
    assert resolve("dilbeek", entity_type="MUNICIPALITY")[0]["id"] == "NEIGHBOUR_MUNICIPALITY:23009"


def test_chien_vert_stop_wins_with_stop_type(db):
    from app.geo.resolver import resolve
    top = resolve("chien vert", entity_type="TRANSIT_STOP")[0]
    assert top["type"] == "TRANSIT_STOP"


def test_new_types_are_known_to_the_detector_and_area_logic():
    from app.geo.detector import ENTITY_TYPES
    from app.tools.geo_tools import AREA_TYPES
    assert {"TRANSIT_LINE", "RAILWAY"} <= set(ENTITY_TYPES)
    assert "NEIGHBOUR_MUNICIPALITY" in AREA_TYPES
