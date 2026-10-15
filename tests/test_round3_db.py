"""
Round-3 tests (everything the revision of round 2 asked for), on the synthetic city. Each test names the revision item.
"""
import json
import re

import psycopg2
import pytest

from app.geo.mapviz import plan_geometry
from app.geo.parsing import repair_extraction
from app.geo.resolver import resolve
from app.tools.geo_tools import plan_from_extraction
from synth_urbis import OX, OY, dsn, make_db

ASSETS = {
    # Avenue Large (y=750) between Avenue de Jette (x=1000) and Rue Gamma (x=1500): carriageway, sidewalks, parking, lane, side street
    "large_carriage": (1100, 750), "large_sidewalk": (1100, 739), "large_parking": (1100, 768), "large_lane": (1100, 780),
    "stub_arm_near": (1250, 800), "stub_arm_far": (1250, 900),
    # Marcel Thiry 52 (y=1650) -> De Cuyper 20 (x=1850): the crossing whole, the arms beyond it not
    "xing": (1752, 1738), "mt_1700": (1750, 1700), "mt_1600": (1750, 1600), "mt_north": (1750, 1850),
    "cuyper_east": (1800, 1750), "cuyper_far": (1950, 1750), "cuyper_west": (1650, 1750),
    # Gamma: the section 1515-1600 carries another street id (99) than the axes (13)
    "gamma_99": (1500, 1550), "gamma_1650": (1500, 1650), "nin_side": (1600, 1500),
    # Place Compact (1060..1140 x 560..640), end of a route along Rue de la Loi
    "place_inner": (1070, 570), "loi_700": (700, 500),
}


@pytest.fixture(scope="module")
def db(synth_db):
    conn = psycopg2.connect(dsn(synth_db))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS public.assets3; CREATE TABLE public.assets3 (id serial, label text, geom geometry(Point,3812))")
    for label, (x, y) in ASSETS.items():
        cur.execute("INSERT INTO public.assets3 (label, geom) VALUES (%s, ST_SetSRID(ST_MakePoint(%s,%s),3812))", (label, OX + x, OY + y))
    yield cur
    conn.close()


def run(db, prompt, raw_extraction):
    extraction = repair_extraction(prompt, raw_extraction)
    res = plan_from_extraction(prompt, "public.assets3", extraction, autonomy_level=3)
    assert res["status"] == "ok", res
    db.execute(res["sql"])
    return res, {r[1] for r in db.fetchall()}


def ents(*items):
    return [{"text": t, "type": ty, "role": ro} for t, ty, ro in items]


# ============================================================ SRID (14 of 28 prompts failed)
def test_srid_audit_all_3812(db):
    db.execute("SELECT DISTINCT srid FROM urbis.srid_audit()")
    assert {r[0] for r in db.fetchall()} == {3812}


@pytest.mark.parametrize("wkt,srid,expect_srid,expect_x_gt", [
    ("POINT(150000 170000)", 0, 3812, 600000),     # SRID 0, Belgian Lambert 72 coordinates -> transformed
    ("POINT(4.35 50.85)", 0, 3812, 600000),         # SRID 0, lon/lat
    ("POINT(649000 670000)", 0, 3812, 600000),      # SRID 0, already 3812 coordinates -> only the SRID is set
    ("POINT(150000 170000)", 31370, 3812, 600000),  # a different SRID -> transformed
    ("POINT(649000 670000)", 3812, 3812, 600000),   # already right
])
def test_to3812(db, wkt, srid, expect_srid, expect_x_gt):
    db.execute("SELECT ST_SRID(urbis.to3812(ST_SetSRID(ST_GeomFromText(%s), %s))), ST_X(urbis.to3812(ST_SetSRID(ST_GeomFromText(%s), %s)))", (wkt, srid, wkt, srid))
    got_srid, x = db.fetchone()
    assert got_srid == expect_srid and x > expect_x_gt


def test_to3812_empty_and_null(db):
    db.execute("SELECT ST_SRID(urbis.to3812('POLYGON EMPTY'::geometry)), urbis.to3812(NULL) IS NULL")
    assert db.fetchone() == (3812, True)


def test_valid_poly_never_loses_srid_and_never_returns_empty(db):
    db.execute("""SELECT ST_SRID(urbis.valid_poly(ST_GeomFromText('POLYGON((0 0,10 10,10 0,0 10,0 0))', 3812))),
                         urbis.valid_poly(ST_GeomFromText('POLYGON((0 0,1 1,2 2,0 0))', 3812)) IS NULL""")
    assert db.fetchone() == (3812, True)     # a sliver that cannot be repaired becomes NULL (ignored by ST_Union), not an SRID-0 empty


def test_plan_results_are_3812_and_map_geojson_works(db):
    res, _ = run(db, "Avenue Louise.", {"entities": ents(("Avenue Louise", "STREET", "anchor")), "relationship": "NONE"})
    db.execute("SELECT ST_SRID(target), ST_SRID(path) FROM urbis.geo_plan(%s::jsonb)", (json.dumps(res["plan"]),))
    assert db.fetchone()[0] == 3812
    g = plan_geometry(res["plan"])
    assert g["target"]["type"] in ("Polygon", "MultiPolygon")


@pytest.fixture(scope="module")
def srid0_db():
    """A complete second database in which EVERY stored geometry has SRID 0 (what the real database showed)."""
    make_db("geotest_srid0", corrupt_srid=True)
    conn = psycopg2.connect(dsn("geotest_srid0"))
    conn.autocommit = True
    yield conn.cursor()
    conn.close()


def test_srid0_database_is_repaired_by_the_sql_layer(srid0_db):
    cur = srid0_db
    cur.execute("SELECT DISTINCT srid FROM urbis.srid_audit()")
    assert {r[0] for r in cur.fetchall()} == {3812}                 # tables AND views
    cur.execute("SELECT count(*) FROM urbis.normalize_srids() WHERE action <> 'ok'")
    assert cur.fetchone()[0] == 0                                   # idempotent: nothing left to convert
    plan = {"rel": "SELF", "ents": [{"gid": "STREET:751", "type": "STREET", "role": "anchor"}]}
    cur.execute("SELECT ST_SRID(target), ST_Area(target), ST_X(ST_Transform(ST_Centroid(target), 4326)), ST_Y(ST_Transform(ST_Centroid(target), 4326)) "
                "FROM urbis.geo_plan(%s::jsonb)", (json.dumps(plan),))
    srid, area, lon, lat = cur.fetchone()
    assert srid == 3812 and 58000 < area < 62000                     # 2000 m x 30 m (Lambert 72 -> 2008 changes it by well under 1 %)
    assert 4.2 < lon < 4.5 and 50.7 < lat < 51.0                     # the SRID-0 Lambert-72 coordinates ended up in Brussels
    plan = {"rel": "WITHIN", "ents": [{"gid": "ADMIN:REGION_RBC", "type": "STREET", "role": "anchor"}, {"gid": "MUNICIPALITY:1", "type": "MUNICIPALITY", "role": "area"}]}
    cur.execute("SELECT ST_Area(target) > 0 FROM urbis.geo_plan(%s::jsonb)", (json.dumps(plan),))   # the 'mixed SRID (0 != 3812)' error of item 8
    assert cur.fetchone()[0]


# ============================================================ cleaning (items 3, 14, 20, 23, 24, 28)
def clean(db, wkt, **kw):
    args = {"close": 2, "hole": 6000, "spur": 0.75, "part": 30, **kw}
    db.execute("SELECT ST_AsText(urbis.geo_clean(ST_GeomFromText(%s, 3812), %s, %s, %s, %s)), ST_SRID(urbis.geo_clean(ST_GeomFromText(%s, 3812)))",
               (wkt, args["close"], args["hole"], args["spur"], args["part"], wkt))
    return db.fetchone()


def area(db, wkt):
    db.execute("SELECT ST_Area(ST_GeomFromText(%s)), ST_NumGeometries(ST_GeomFromText(%s)), ST_NumInteriorRings(ST_GeometryN(ST_GeomFromText(%s), 1))", (wkt, wkt, wkt))
    return db.fetchone()


def test_clean_closes_hairline_gaps_between_polygons(db):
    two = "MULTIPOLYGON(((0 0,50 0,50 20,0 20,0 0)),((50.8 0,100 0,100 20,50.8 20,50.8 0)))"       # 0.8 m gap: drawn as a line inside the result
    wkt, srid = clean(db, two)
    a, n, rings = area(db, wkt)
    assert n == 1 and rings == 0 and 1995 <= a <= 2005 and srid == 3812


def test_clean_fills_small_donuts_but_keeps_big_holes(db):
    small = "POLYGON((0 0,100 0,100 100,0 100,0 0),(40 40,60 40,60 60,40 60,40 40))"                   # 400 m2 hole
    big = "POLYGON((0 0,300 0,300 300,0 300,0 0),(50 50,250 50,250 250,50 250,50 50))"                 # 40 000 m2 hole
    assert area(db, clean(db, small)[0])[2] == 0
    assert area(db, clean(db, big)[0])[2] == 1


def test_clean_removes_thin_spurs_and_tiny_islands(db):
    spur = "POLYGON((0 0,100 0,100 40,50.3 40,50.3 80,49.7 80,49.7 40,0 40,0 0))"                      # a 0.6 m wide, 40 m long thread
    a, n, _ = area(db, clean(db, spur)[0])
    assert 3900 < a < 4100                                                                              # 100 x 40 = 4000, the thread is gone
    island = "MULTIPOLYGON(((0 0,100 0,100 100,0 100,0 0)),((300 300,304 300,304 304,300 304,300 300)))"
    assert area(db, clean(db, island)[0])[1] == 1


def test_clean_leaves_lines_points_and_the_only_part_alone(db):
    assert clean(db, "LINESTRING(0 0,10 10)")[0] == "LINESTRING(0 0,10 10)"
    tiny = "POLYGON((0 0,5 0,5 5,0 5,0 0))"                                                             # 25 m2 < part limit, but it is the only part
    assert area(db, clean(db, tiny)[0])[0] > 0


def test_final_results_are_clean_end_to_end(db):
    """no interior rings smaller than the donut limit, single piece, in the result of a real route"""
    res, _ = run(db, "Rue Alpha entre la rue de la loi et avenue louise",
                 {"entities": ents(("Rue Alpha", "STREET", "start"), ("rue de la loi", "STREET", "end"), ("avenue louise", "STREET", "via")), "relationship": "BETWEEN"})
    db.execute("""SELECT ST_NumGeometries(target), (SELECT count(*) FROM ST_DumpRings(ST_GeometryN(target, 1)) WHERE path[1] > 0) FROM urbis.geo_plan(%s::jsonb)""", (json.dumps(res["plan"]),))
    n, rings = db.fetchone()
    assert n == 1 and rings == 0


# ============================================================ width of the street, junctions whole, side streets not (items 5, 6, 20, 23, 24)
def test_item5_whole_width_of_the_street(db):
    res, got = run(db, "trous entre l'avenue de jette et la rue gamma sur l'avenue large",
                   {"entities": ents(("avenue large", "STREET", "main"), ("avenue de jette", "STREET", "start"), ("rue gamma", "STREET", "end")), "relationship": "BETWEEN"})
    assert {"large_carriage", "large_sidewalk", "large_parking", "large_lane"} <= got        # sidewalks, unnamed parking and the lane of ANOTHER street id
    assert "stub_arm_near" not in got and "stub_arm_far" not in got                           # the side street is not part of the avenue


def test_item23_crossing_whole_but_not_the_arms_beyond_it(db):
    res, got = run(db, "Avenue Marcel Thiry 52 a Avenue Theodore De Cuyper 20",
                   {"entities": ents(("Avenue Marcel Thiry 52", "ADDRESS", "start"), ("Avenue Théodore De Cuyper 20", "ADDRESS", "end")), "relationship": "FROM_TO"})
    assert {"mt_1700", "xing", "cuyper_east"} <= got                  # route + the WHOLE crossing
    assert "mt_north" not in got and "cuyper_west" not in got         # the road continuing north / west of the crossing is NOT included
    assert "mt_1600" not in got and "cuyper_far" not in got           # cut flat at the two house numbers


def test_item28_no_gaps_when_surface_street_ids_differ_from_the_axes(db):
    res, got = run(db, "de l'avenue louise a l'avenue theodore de cuyper",
                   {"entities": ents(("avenue louise", "STREET", "start"), ("avenue theodore de cuyper", "STREET", "end")), "relationship": "BETWEEN"})
    assert {"gamma_99", "gamma_1650"} <= got                          # the section named 'Rue Autre' (id 99) is part of the track
    assert "nin_side" not in got                                      # the Ninoofsesteenweg crossing the track is a side street


def test_item5_square_at_the_end_is_whole(db):
    res, got = run(db, "entre la rue alpha et la place compact",
                   {"entities": ents(("rue alpha", "STREET", "start"), ("place compact", "STREET", "end")), "relationship": "BETWEEN"})
    assert "place_inner" in got and "loi_700" in got                  # the whole square, reached along the main road


# ============================================================ names (items 9, 13, 18)
def test_item18_spelling_variants_are_one_street(db):
    db.execute("SELECT gid, pids FROM urbis.street_groups WHERE 80 = ANY(pids) OR 81 = ANY(pids) OR 82 = ANY(pids)")
    rows = db.fetchall()
    assert len(rows) == 1 and sorted(rows[0][1]) == [80, 81, 82]       # 'Tervueren' / 'Tervuren' (FR) + 'Tervurenlaan' (NL), 240 m apart


@pytest.mark.parametrize("text", ["avenue de tervuren", "Avenue de Tervueren", "tervurenlaan"])
def test_item18_every_spelling_finds_the_same_street(db, text):
    c = resolve(text, "STREET")
    assert c and c[0]["id"] == "STREET:80" and not [x for x in c if "#" in x["id"]]


def test_item13_quartier_prefers_the_district_over_the_statistical_sector(db):
    c = resolve("Murtsaard", "MUNICIPALITY", hint="AREA")
    assert c[0]["type"] == "MONITORING_DISTRICT"


def test_same_name_street_pieces_split_by_a_gap_are_still_resolved_to_one_street(db):
    """Rue Gamma: surfaces of another street id in the middle must not split it into two competing candidates"""
    db.execute("SELECT count(*) FROM urbis.street_groups WHERE 13 = ANY(pids)")
    assert db.fetchone()[0] == 1


# ============================================================ PostgreSQL 17: CREATE / REFRESH MATERIALIZED VIEW run with search_path = pg_catalog, pg_temp
def test_views_and_functions_survive_the_restricted_search_path_of_postgresql_17(synth_db):
    """
    PG17 populates/refreshes materialized views with a restricted search_path. An inlined SQL function whose body calls PostGIS
    unqualified (ST_SRID ...) then fails with 'function st_srid(public.geometry) does not exist' -- seen on the real PG17 database,
    invisible on PG16. Every function therefore carries its own SET search_path. This test emulates the restricted path.
    """
    conn = psycopg2.connect(dsn(synth_db))
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SET search_path = pg_catalog, pg_temp")
        for mv in ("v_street_axes_routing", "street_axes_nodes", "v_route_edges", "route_nodes", "v_rail_edges", "rail_nodes",
                   "street_groups", "gazetteer", "road_class_gazetteer", "mv_addresses_lookup"):
            cur.execute(f"REFRESH MATERIALIZED VIEW urbis.{mv}")                       # raised the 'st_srid' error before the fix
        cur.execute("SELECT urbis.to3812(geometry) IS NOT NULL FROM urbis.region LIMIT 1")
        assert cur.fetchone()[0]
        plan = {"rel": "SELF", "ents": [{"gid": "STREET:751", "type": "STREET", "role": "anchor"}]}
        cur.execute("SELECT public.ST_Area(target) FROM urbis.geo_plan(%s::jsonb)", (json.dumps(plan),))
        assert 59000 < cur.fetchone()[0] < 61000
    finally:
        conn.close()


def test_every_function_has_its_own_search_path(synth_db):
    conn = psycopg2.connect(dsn(synth_db))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                   WHERE n.nspname = 'urbis' AND p.prokind = 'f' AND (p.proconfig IS NULL OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'))""")
    missing = [r[0] for r in cur.fetchall()]
    conn.close()
    assert not missing, f"functions without SET search_path: {missing}"


# ============================================================ robustness against PostGIS/GEOS builds that lose an SRID or return invalid polygons
def test_house_number_cut_survives_a_cut_geometry_without_srid(db):
    """'mixed SRID geometries (3812 != 0)' in geo_surfaces_along on the real database: a cut that lost its SRID must not fail the prompt"""
    db.execute("""SELECT edge_ids, path, cuts FROM urbis.geo_route(
        jsonb_build_array(jsonb_build_object('wkt', ST_AsText(ST_SetSRID(ST_MakePoint(%s, %s), 3812)), 'type', 'ADDRESS', 'role', 'start'),
                          jsonb_build_object('wkt', ST_AsText(ST_SetSRID(ST_MakePoint(%s, %s), 3812)), 'type', 'ADDRESS', 'role', 'end')), NULL, 'ROUTE')""",
               (OX + 512, OY + 100, OX + 512, OY + 450))
    edge_ids, path, cuts = db.fetchone()
    assert cuts is not None
    db.execute("SELECT ST_Area(urbis.geo_surfaces_along(%s::geometry, %s::bigint[], 40, ST_SetSRID(%s::geometry, 0)))", (path, edge_ids, cuts))
    assert db.fetchone()[0] > 0                                           # worked although the cut had SRID 0


def test_intersection_ratio_never_raises_on_invalid_polygons(db):
    db.execute("""SELECT urbis._geo_ratio(ST_GeomFromText('POLYGON((0 0,10 10,10 0,0 10,0 0))', 3812),         -- bow-tie
                                          ST_GeomFromText('POLYGON((0 0,20 0,20 20,0 20,0 0))', 3812)),
                         urbis._geo_ratio(ST_GeomFromText('POLYGON((0 0,10 0,10 10,0 10,0 0))', 3812), NULL)""")
    a, b = db.fetchone()
    assert a is not None and b == 0


def test_s_restamps_a_lost_srid(db):
    db.execute("SELECT ST_SRID(urbis._s(ST_SetSRID(ST_MakePoint(1, 1), 0)))")
    assert db.fetchone()[0] == 3812
