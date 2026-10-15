"""
End-to-end tests of everything AFTER the LLM, on the synthetic city (tests/synth_urbis.py):
raw extraction (with the same kinds of mistakes the real LLM made in round 1) -> repair -> resolve -> plan -> SQL,
then the generated SQL is EXECUTED against a table of test points and the selected labels are compared.
Each test names the revision item it covers.
"""
import json

import psycopg2
import pytest

from app.geo.parsing import repair_extraction
from app.geo.mapviz import plan_geometry
from app.tools.geo_tools import plan_from_extraction
from synth_urbis import OX, OY, dsn

ASSETS = {  # label: (x, y)  -- metres in the synthetic city
    # Avenue Louise (y=1000) and the side street Rue Delta (x=750, y>1000)
    "louise_w": (300, 1000), "louise_e": (1700, 1005), "delta_side": (750, 1500),
    # Rue Alpha (x=500), house numbers 29 (y=100) .. 90 (y=450); Rue de la Loi crosses at y=500
    "alpha_50": (500, 50), "alpha_300": (500, 300), "alpha_460": (500, 460), "alpha_700": (500, 700), "loi_700": (700, 500),
    # Marcel Thiry (x=1750) 52 is at y=1650; Theodore De Cuyper (y=1750)
    "mt_1600": (1750, 1600), "mt_1700": (1750, 1700), "cuyper_1650": (1650, 1750),
    # Avenue de Jette (x=1000): tram axes above y=1000
    "jette_tram": (1000, 1200), "jette_notram": (1000, 600), "jette_next": (1100, 1200),
    # tram line 8 (y=300): Etterbeek (x<1000) / Oudergem (x>1000)
    "tram_oud": (1500, 310), "tram_ett": (400, 310), "tram_far": (1500, 330),
    # Ninoofsesteenweg y=1500, Weststation (1250,1480), R0 at x=2300 (east) and x=-400 (west)
    "nin_w800": (800, 1500), "nin_e1800": (1800, 1500), "nin_belt2200": (2200, 1500), "nin_beyond_r0": (2400, 1500),
    # railway y=200 between Gare de Jette (400) and Bockstael (1400)
    "rail_700": (700, 205), "rail_200": (200, 205), "rail_branch": (1000, 390),
    # zones
    "ett_in": (100, 100), "oud_in": (1500, 100),
    # Rue de la Loi, between Alpha (x=500) and Gamma (x=1500)
    "loi_mid": (1000, 505), "loi_w": (200, 500), "beta_side": (1000, 250),
}


@pytest.fixture(scope="module")
def db(synth_db):
    conn = psycopg2.connect(dsn(synth_db))
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS public.assets; CREATE TABLE public.assets (id serial, label text, geom geometry(Point,3812))")
    for label, (x, y) in ASSETS.items():
        cur.execute("INSERT INTO public.assets (label, geom) VALUES (%s, ST_SetSRID(ST_MakePoint(%s,%s),3812))", (label, OX + x, OY + y))
    yield cur
    conn.close()


def run(db, prompt, raw_extraction):
    extraction = repair_extraction(prompt, raw_extraction)
    res = plan_from_extraction(prompt, "public.assets", extraction, autonomy_level=3)
    assert res["status"] == "ok", res
    db.execute(res["sql"])
    return res, {r[1] for r in db.fetchall()}


def ents(*items):
    return [{"text": t, "type": ty, "role": ro} for t, ty, ro in items]


# ------------------------------------------------------------------ streets
def test_item27_street_itself_no_side_streets(db):
    res, got = run(db, "Avenue Louise.", {"entities": ents(("Avenue Louise", "STREET", "anchor")), "relationship": "NONE"})
    assert res["relationship"] == "SELF"
    assert got == {"louise_w", "louise_e"}                       # all three placename ids, not the side street Rue Delta
    g = plan_geometry(res["plan"])
    assert g["mode"] == "geometry"
    assert "delta_side" not in got and "loi_700" not in got


def test_item2_all_segments_of_louise_even_though_three_placename_ids(db):
    res, got = run(db, "potholes on avenue louise", {"entities": ents(("avenue louise", "STREET", "anchor")), "relationship": "ON"})
    assert {"louise_w", "louise_e"} <= got                        # west part is placename 751, east part 3919


def test_item16_tram_street_only_axes_with_tram_and_road_alongside(db):
    res, got = run(db, "L'avenue de jette avec un tram", {"entities": ents(("Avenue de Jette", "STREET", "anchor"), ("tram", "TRANSIT_LINE", "anchor")), "relationship": "WITHIN"})
    assert res["plan"]["modifier"] == "TRAM"
    assert got == {"jette_tram"}                                  # not y<1000 (no tram), not the adjacent road
    assert plan_geometry(res["plan"])["mode"] == "tram_axes"


# ------------------------------------------------------------------ zones, classes, lines
def test_item16_tram_modifier_even_if_llm_omits_the_tram_entity(db):
    res, got = run(db, "L'avenue de jette avec un tram", {"entities": ents(("Avenue de Jette", "STREET", "anchor")), "relationship": "WITHIN"})
    assert res["plan"]["modifier"] == "TRAM" and got == {"jette_tram"}


def test_item25_zone_no_buffer(db):
    res, got = run(db, "Les voiries à Etterbeek", {"entities": ents(("Etterbeek", "MUNICIPALITY", "anchor")), "relationship": "NONE"})
    assert res["plan"]["buffer"] is None and "ett_in" in got and "oud_in" not in got


def test_item8_regional_roads_in_municipality(db):
    res, got = run(db, "crossings on regional roads in Etterbeek",
                   {"entities": ents(("regional roads", "STREET", "anchor"), ("Etterbeek", "MUNICIPALITY", "anchor")), "relationship": "WITHIN"})
    assert any(e["match"]["id"].startswith("ADMIN:") for e in res["entities"])
    assert "tram_oud" not in got and "mt_1700" not in got and "oud_in" not in got   # outside Etterbeek


def test_item12_tram_line_in_municipality_default_20m(db):
    res, got = run(db, "arbres autour de la ligne de tram 8 a oudergem",
                   {"entities": ents(("ligne de tram 8", "TRANSIT_LINE", "anchor"), ("Ouderkerken", "MUNICIPALITY", "anchor")), "relationship": "NEAR"})
    assert res["plan"]["buffer"] == 20 and res["distance_meters"] == 20
    assert got == {"tram_oud"}                                    # not Etterbeek's part of the line, not >20 m away


def test_item12_stated_buffer_wins_over_default(db):
    res, got = run(db, "arbres a 40 m de la ligne de tram 8 a oudergem",
                   {"entities": ents(("ligne de tram 8", "TRANSIT_LINE", "anchor"), ("oudergem", "MUNICIPALITY", "anchor")), "relationship": "NEAR", "distance_meters": None})
    assert res["plan"]["buffer"] == 40 and {"tram_oud", "tram_far"} <= got


# ------------------------------------------------------------------ BETWEEN
def test_item5_route_between_axes_of_two_streets_prefers_main_road(db):
    res, got = run(db, "potholes between rue alpha and rue gamma",
                   {"entities": ents(("rue alpha", "STREET", "start"), ("rue gamma", "STREET", "end")), "relationship": "BETWEEN"})
    g = plan_geometry(res["plan"])
    assert g["mode"] == "route"
    assert got == {"loi_mid", "loi_700"}                          # along the cheaper Metropolitan road (Rue de la Loi), not Louise


def test_item20_x_between_y_and_z_only_x(db):
    res, got = run(db, "Rue Alpha entre la rue de la loi et avenue louise",
                   {"entities": ents(("Rue Alpha", "STREET", "start"), ("rue de la loi", "STREET", "end"), ("avenue louise", "STREET", "via")), "relationship": "BETWEEN"})
    assert [e["role"] for e in res["plan"]["ents"]][:1] == ["main"]
    assert got == {"alpha_700"}                                   # only Rue Alpha between its crossings with Loi (y=500) and Louise (y=1000)
    assert "loi_700" not in got and "louise_w" not in got and "alpha_460" not in got


def test_items14_22_numbers_on_one_street_cut_at_the_numbers(db):
    res, got = run(db, "Rue Alpha tussen nummer 90 en 29",
                   {"entities": ents(("Rue Alpha 90", "ADDRESS", "start"), ("Rue Alpha 29", "ADDRESS", "end")), "relationship": "BETWEEN"})
    assert res["plan"]["ents"][0]["role"] == "main"               # inferred from the two addresses
    assert got == {"alpha_300"}                                   # not y=50, not y=460, not the crossing Rue de la Loi
    sel = [e for e in res["entities"] if e["chosen"]["type"] == "ADDRESS"]
    assert all(e["chosen"]["number_exact"] for e in sel)


def test_item6_address_to_crossing_street(db):
    res, got = run(db, "road quality on Marcel thiry between number 52 and Theodere Decuyper",
                   {"entities": ents(("Marcel thiry", "STREET", "start"), ("Theodere Decuyper", "STREET", "end")), "relationship": "BETWEEN"})
    ad = next(e for e in res["entities"] if e["role"] == "start")
    assert ad["chosen"]["type"] == "ADDRESS" and ad["chosen"]["matched_number"] == "52" and ad["chosen"]["number_exact"]
    assert got == {"mt_1700"}                                     # from number 52 (y=1650) to the crossing (y=1750): not before 52, not on De Cuyper


def test_item9_street_between_belt_road_and_station(db):
    res, got = run(db, "verkeerslichten op de ninoofsesteenweg tussen de ring en weststation",
                   {"entities": ents(("ninoofsesteenweg", "STREET", "main"), ("ring", "POI", "start"), ("weststation", "POI", "end")), "relationship": "BETWEEN"})
    g = plan_geometry(res["plan"])
    assert g["mode"] == "route_on_main_street", g
    assert got == {"nin_e1800", "nin_belt2200"}                   # towards the NEAREST R0 crossing, in the Region AND in the belt
    assert "nin_w800" not in got and "nin_beyond_r0" not in got


def test_item21_railway_between_two_points_buffered_20m(db):
    res, got = run(db, "Spoorweg tussen gare de jette en bockstael",
                   {"entities": ents(("gare de jette", "POI", "start"), ("bockstael", "STREET", "end")), "relationship": "BETWEEN"})
    assert res["plan"]["ents"][0]["type"] == "RAILWAY" and res["plan"]["buffer"] == 20
    assert got == {"rail_700"}


def test_item21_stated_buffer_for_railway(db):
    res, got = run(db, "Spoorweg tussen gare de jette en bockstael, buffer 250 m",
                   {"entities": ents(("gare de jette", "POI", "start"), ("bockstael", "STREET", "end")), "relationship": "BETWEEN"})
    assert res["plan"]["buffer"] == 250 and {"rail_700", "rail_branch"} <= got


# ------------------------------------------------------------------ resolution priorities
def test_item3_stop_beats_street_for_bare_name(db):
    res, _ = run(db, "problematic drains between chien vert and the stocklet palace",
                 {"entities": ents(("chien vert", "STREET", "start"), ("stocklet palace", "POI", "end")), "relationship": "BETWEEN"})
    assert res["entities"][0]["chosen"]["type"] == "TRANSIT_STOP"


def test_item1_highroad_is_a_road_class_not_a_poi(db):
    res, _ = run(db, "potholes near highroad", {"entities": ents(("highroad", "POI", "anchor")), "relationship": "NEAR"})
    assert res["entities"][0]["chosen"]["id"] == "HIERARCHY:H"


def test_items_7_26_squares_resolve_to_street_segments(db):
    r7, _ = run(db, "all objects in the zone meiser", {"entities": ents(("zone meiser", "POI", "anchor")), "relationship": "NONE"})
    r26, _ = run(db, "La grand place.", {"entities": ents(("La Grand Place", "POI", "anchor")), "relationship": "NONE"})
    assert r7["entities"][0]["chosen"]["type"] == "STREET" and r26["entities"][0]["chosen"]["type"] == "STREET"


def test_item13_quartier_resolves_to_district_polygon(db):
    res, _ = run(db, "les voirie du quartier Murtsaard", {"entities": ents(("Murtsaard", "MUNICIPALITY", "anchor")), "relationship": "NONE"})
    assert res["entities"][0]["chosen"]["type"] == "MONITORING_DISTRICT" and res["plan"]["buffer"] is None


def test_item19_gate_override_short_place_name(db, monkeypatch):
    from app.tools import geo_tools
    monkeypatch.setattr(geo_tools, "detect", lambda q: {"has_spatial": False, "relationship": "NONE"})
    monkeypatch.setattr(geo_tools, "audit", lambda *a, **k: None)
    res = geo_tools.geo_pipeline("Edmont machtens", "public.assets", autonomy_level=3)
    assert res["relationship"] == "SELF" and res["entities"][0]["chosen"]["id"] == "STREET:55"


def test_gate_does_not_fire_for_questions(db, monkeypatch):
    from app.tools import geo_tools
    monkeypatch.setattr(geo_tools, "detect", lambda q: {"has_spatial": False, "relationship": "NONE"})
    monkeypatch.setattr(geo_tools, "audit", lambda *a, **k: None)
    assert geo_tools.geo_pipeline("what is the capital of Belgium?", "public.assets", autonomy_level=3)["sql"] == "SELECT * FROM public.assets"


def test_generated_sql_is_short_and_calls_the_function(db):
    res, _ = run(db, "Avenue Louise.", {"entities": ents(("Avenue Louise", "STREET", "anchor")), "relationship": "NONE"})
    assert "urbis.geo_target(" in res["sql"] and len(res["sql"]) < 600


def test_refresh_gazetteer_procedure_works_with_new_views(db):
    """CONCURRENTLY refresh needs the unique indexes on v_route_edges / street_groups / route_nodes / rail_nodes."""
    db.execute("CALL urbis.refresh_gazetteer()")
    db.execute("SELECT count(*) FROM urbis.street_groups")
    assert db.fetchone()[0] >= 10
    db.execute("SELECT count(*) FROM urbis.gazetteer WHERE type = 'STREET' AND id NOT LIKE '%#%'")   # alias rows (other spellings) excluded
    n_streets = db.fetchone()[0]
    db.execute("SELECT count(*) FROM urbis.street_groups")
    assert n_streets == db.fetchone()[0]


def test_street_groups_merge_ids_and_keep_side_streets_out(db):
    db.execute("""SELECT pids, round(ST_Area(geom)) FROM urbis.street_groups WHERE 751 = ANY(pids)""")
    pids, area = db.fetchone()
    assert sorted(pids) == [751, 1858, 3919] and area == 2000 * 30           # 2000 m x 30 m: no side street polygon in it
    db.execute("""SELECT round(ST_Area(geom)) FROM urbis.street_groups WHERE 14 = ANY(pids)""")
    assert db.fetchone()[0] > 0                                               # Rue Delta is its own street


def test_same_name_streets_in_different_places_stay_separate(db):
    """Two 'Rue de la Station' 1 km apart are two streets (cluster distance 60 m), not one giant geometry."""
    db.execute("INSERT INTO urbis.placenames VALUES (900,'Rue de la Station','Stationsstraat')")
    db.execute("INSERT INTO urbis.streetsurfaces VALUES (9001, 900, 1, 6, 1, 1, ST_GeomFromText(%s,3812)), (9002, 900, 1, 6, 1, 1, ST_GeomFromText(%s,3812))",
               (f"POLYGON(({OX+100} {OY+1900},{OX+300} {OY+1900},{OX+300} {OY+1910},{OX+100} {OY+1910},{OX+100} {OY+1900}))",
                f"POLYGON(({OX+1700} {OY+1900},{OX+1900} {OY+1900},{OX+1900} {OY+1910},{OX+1700} {OY+1910},{OX+1700} {OY+1900}))"))
    db.execute("REFRESH MATERIALIZED VIEW urbis.street_groups")
    db.execute("SELECT count(*) FROM urbis.street_groups WHERE 900 = ANY(pids)")
    assert db.fetchone()[0] == 2
    db.execute("DELETE FROM urbis.streetsurfaces WHERE inspire_id IN (9001, 9002); DELETE FROM urbis.placenames WHERE inspire_id = 900")
    db.execute("REFRESH MATERIALIZED VIEW urbis.street_groups")
