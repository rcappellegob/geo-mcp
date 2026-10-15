"""Pure-Python tests of app/geo/parsing.py. The cases are the real LLM extractions of revision round 1
(tests/data/round1_llm_extractions.json) -- each assertion encodes a decision from the revision notes."""
import json
from pathlib import Path

import pytest

from app.geo.parsing import extract_distance, parse_address, repair_extraction, snap_to_prompt, strip_generic

DATA = {str(x["id"]): x for x in json.loads((Path(__file__).parent / "data" / "round1_llm_extractions.json").read_text())}


def fix(pid):
    x = DATA[str(pid)]
    return repair_extraction(x["prompt"], x["extraction"])


def roles(r):
    return {e["text"].lower(): e["role"] for e in r["entities"]}


# ---- addresses -------------------------------------------------------------
@pytest.mark.parametrize("text,street,number,muni", [
    ("Marcel Thiry 52", "Marcel Thiry", "52", None),
    ("Avenue Marcel Thiry 52", "Avenue Marcel Thiry", "52", None),
    ("Rue Stuyvenbergh, 29", "Rue Stuyvenbergh", "29", None),
    ("29 Rue Stuyvenbergh", "Rue Stuyvenbergh", "29", None),
    ("Rue Théophile De Baisieux, 134 - Jette", "Rue Théophile De Baisieux", "134", "Jette"),
    ("Rue de la Loi 12A, 1000 Bruxelles", "Rue de la Loi", "12A", "Bruxelles"),
    ("Avenue Louise n° 54", "Avenue Louise", "54", None),
])
def test_parse_address(text, street, number, muni):
    a = parse_address(text)
    assert a and a["street"] == street and a["number"] == number and a["municipality"] == muni


@pytest.mark.parametrize("text", ["Rue de la Loi", "Avenue Louise", "Berchem", "tram 8", "Rue du 11 Novembre"])
def test_parse_address_no_number(text):
    assert parse_address(text) is None


def test_item23_municipality_belongs_to_address_not_waypoint():
    r = fix(23)
    assert all(e["type"] == "ADDRESS" for e in r["entities"])
    assert [e["address"]["number"] for e in r["entities"]] == ["29", "134"]
    assert {e["address"]["municipality"] for e in r["entities"]} == {"Jette"}
    assert not any(e["type"] == "MUNICIPALITY" for e in r["entities"])


def test_item14_and_22_numbers_on_one_street():
    for pid, nums in ((14, ["48", "82"]), (22, ["90", "29"])):
        r = fix(pid)
        assert [e["address"]["number"] for e in r["entities"]] == nums
        assert len({e["address"]["street"].lower().split()[-1] for e in r["entities"]}) == 1


def test_item6_number_attached_to_preceding_street():
    r = fix(6)
    start = next(e for e in r["entities"] if e["role"] == "start")
    assert start["type"] == "ADDRESS" and start["address"]["number"] == "52"
    assert next(e for e in r["entities"] if e["role"] == "end")["type"] == "STREET"


# ---- frames / roles --------------------------------------------------------
def test_item9_and_20_x_between_y_and_z():
    r9 = roles(fix(9))
    assert r9["ninoofsesteenweg"] == "main" and r9["ring"] == "start" and r9["weststation"] == "end"
    r20 = roles(fix(20))   # the LLM had Kasteellaan as 'start'
    assert r20["kasteellaan"] == "main" and r20["basilieklaan"] == "start" and r20["edouard bènèslaan"] == "end"


def test_item24_via():
    r = fix(24)
    assert r["relationship"] in ("FROM_TO", "BETWEEN")
    assert roles(r) == {"square eugène plasky": "start", "place de la patrie": "end", "place meiser": "via"}


def test_item21_railway_main_entity_added():
    r = fix(21)
    assert any(e["type"] == "RAILWAY" and e["role"] == "main" for e in r["entities"])


# ---- relationship / distance -----------------------------------------------
@pytest.mark.parametrize("pid", [7, 11, 13, 15, 17, 25, 26, 27])
def test_bare_place_name_is_self(pid):
    r = fix(pid)
    assert r["relationship"] == "SELF" and len(r["entities"]) == 1


def test_item10_buffer_distance():
    r = fix(10)
    assert r["relationship"] == "NEAR" and r["distance_meters"] == 500


def test_item16_generic_tram_is_modifier():
    r = fix(16)
    assert r["modifier"] == "TRAM" and [e["text"] for e in r["entities"]] == ["Avenue de Jette"]


def test_item12_numbered_tram_line_stays_a_line():
    r = fix(12)
    assert r["modifier"] is None and any(e["type"] == "TRANSIT_LINE" for e in r["entities"])
    assert [e["text"] for e in r["entities"] if e["type"] == "MUNICIPALITY"] == ["oudergem"]   # LLM wrote "Ouderkerken"


# ---- hints / typos / distances ----------------------------------------------
def test_generic_prefix_hints():
    assert strip_generic("zone meiser") == ("meiser", "PLACE")
    assert strip_generic("quartier de Mutsaard") == ("Mutsaard", "AREA")
    assert strip_generic("Rue de la Loi") == ("Rue de la Loi", None)
    assert fix(7)["entities"][0]["hint"] == "PLACE"
    assert fix(13)["entities"][0]["hint"] == "AREA"      # 'quartier' is in the prompt, not in the LLM's text


def test_snap_to_prompt():
    assert snap_to_prompt("Ouderkerken", "arbres autour de la ligne de tram 8 a oudergem") == "oudergem"
    assert snap_to_prompt("Rue de la Loi", "potholes on rue de la loi") == "Rue de la Loi"
    assert snap_to_prompt("Zzzzz", "potholes on rue de la loi") == "Zzzzz"


@pytest.mark.parametrize("prompt,metres", [("buffer of 500m around X", 500), ("within 1,5 km of X", 1500), ("20 m around the tram", 20),
                                           ("tram 8", None), ("bus 12 to the station", None)])
def test_extract_distance(prompt, metres):
    assert extract_distance(prompt) == metres


def test_between_y_and_z_on_x_main_street_named_last():
    raw = {"entities": [{"text": "avenue large", "type": "STREET", "role": "start"}, {"text": "avenue de jette", "type": "STREET", "role": "end"},
                        {"text": "rue gamma", "type": "STREET", "role": "anchor"}], "relationship": "BETWEEN"}
    r = repair_extraction("trous entre l'avenue de jette et la rue gamma sur l'avenue large", {"entities": [
        {"text": "avenue de jette", "type": "STREET", "role": "start"}, {"text": "rue gamma", "type": "STREET", "role": "end"},
        {"text": "avenue large", "type": "STREET", "role": "anchor"}], "relationship": "BETWEEN"})
    assert {e["text"]: e["role"] for e in r["entities"]} == {"avenue large": "main", "avenue de jette": "start", "rue gamma": "end"}
