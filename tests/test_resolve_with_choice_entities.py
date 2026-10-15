"""
tests/test_resolve_with_choice_entities.py
=============================================
Regression test: resolve_with_choice() must return an "entities" list
carrying each chosen gazetteer id's WKT, mirroring geo_query's own shape.
Without this, chat-platform's map-display logic silently has nothing to
draw after a disambiguation choice is resolved (confirmed in real testing
-- the map stayed empty after picking a clarification option).

Mocks app.tools.geo_tools.query directly -- no live Postgres needed, and
no dependency on app.db's actual connection logic.
"""
from unittest.mock import patch

from app.tools.geo_tools import resolve_with_choice


FAKE_ROWS = [
    {"id": "GAZ:1", "type": "street", "name_fr": "Rue Test", "name_nl": "Testraat", "wkt": "LINESTRING(0 0, 1 1)"},
    {"id": "GAZ:2", "type": "building", "name_fr": "Bâtiment Test", "name_nl": "Testgebouw", "wkt": "POINT(2 2)"},
]


@patch("app.tools.geo_tools.query", return_value=FAKE_ROWS)
@patch("app.tools.geo_tools.audit")
def test_resolve_with_choice_returns_entities_with_wkt(mock_audit, mock_query):
    result = resolve_with_choice(
        table="unassigned_assets", relationship="BETWEEN", gazetteer_ids=["GAZ:1", "GAZ:2"],
    )
    assert result["status"] == "ok"
    assert "entities" in result
    assert len(result["entities"]) == 2
    assert result["entities"][0] == {
        "text": "GAZ:1",
        "match": {"id": "GAZ:1", "type": "street", "name_fr": "Rue Test", "name_nl": "Testraat", "wkt": "LINESTRING(0 0, 1 1)"},
    }
    assert result["entities"][1]["match"]["wkt"] == "POINT(2 2)"


@patch("app.tools.geo_tools.query", return_value=FAKE_ROWS)
@patch("app.tools.geo_tools.audit")
def test_resolve_with_choice_entities_have_readable_names_not_raw_ids(mock_audit, mock_query):
    """
    Regression test for the real bug found in testing: the admin panel
    showed entity_names as raw ids ("STREET:3052", "POI:17424") instead
    of readable place names, because the query originally selected only
    id/type/wkt -- name_fr/name_nl were silently missing, so
    entity_label() fell back to the id.
    """
    result = resolve_with_choice(table="unassigned_assets", relationship="NEAR", gazetteer_ids=["GAZ:1"])
    match = result["entities"][0]["match"]
    assert match["name_fr"] == "Rue Test"
    assert match["name_nl"] == "Testraat"


@patch("app.tools.geo_tools.query", return_value=FAKE_ROWS)
@patch("app.tools.geo_tools.audit")
def test_resolve_with_choice_entities_preserve_requested_order(mock_audit, mock_query):
    # Request in reverse order from how the fake DB rows are listed --
    # entities must follow gazetteer_ids' order, not the query result's.
    result = resolve_with_choice(
        table="unassigned_assets", relationship="NEAR", gazetteer_ids=["GAZ:2", "GAZ:1"],
    )
    assert [e["text"] for e in result["entities"]] == ["GAZ:2", "GAZ:1"]
