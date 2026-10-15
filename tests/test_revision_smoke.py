"""The revision report generator (tools/revision.py) must run on the shared pipeline: stub the LLM, run one traced prompt
against the synthetic city, and render the HTML report + digest."""
import app.tools.revision as rev
from app.geo.parsing import repair_extraction


def test_traced_pipeline_and_report_render(synth_db, monkeypatch):
    prompt = "Rue Alpha tussen nummer 90 en 29"
    raw = {"entities": [{"text": "Rue Alpha 90", "type": "ADDRESS", "role": "start"}, {"text": "Rue Alpha 29", "type": "ADDRESS", "role": "end"}],
           "relationship": "BETWEEN"}
    monkeypatch.setattr(rev, "detect", lambda q: {"has_spatial": True, "relationship": "BETWEEN"})
    monkeypatch.setattr(rev, "extract_entities", lambda q: repair_extraction(q, raw))
    rec = rev.run_traced_pipeline("t1", prompt, table="public.assets", autonomy_level=3)
    assert rec["error"] is None, rec["error"]
    assert rec["relationship"] == "BETWEEN" and rec["sql"] and "urbis.geo_target(" in rec["sql"]
    assert rec["final_geometries"] and rec["intermediate_geometries"] and rec["geometry_mode"] == "route_on_main_street"
    d = rev.digest_record(rec, None, 25.0)
    assert d["status"] == "OK" and not [f for f in d["flags"] if f["code"] in ("ERROR", "UNRESOLVED_ENTITY")]
    html = rev.render_html(rec)
    assert "<html" in html.lower() and "route axes" in html
