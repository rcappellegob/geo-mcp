#!/usr/bin/env python3
"""
examples/demo_orchestrator.py
==============================
Run this to WATCH, step by step, what an orchestrating chatbot does with a
prompt when it has three MCP tools available:

    geo_query      -> geo-mcp        (real pipeline shape: detect / extract / resolve / build)
    query_objects  -> db-mcp         (MOCK -- this server doesn't exist yet)
    create_graph   -> chart-mcp      (MOCK -- this server doesn't exist yet)

Two modes:

  --mode mock (default, zero dependencies, run this first)
      A small deterministic, keyword-based "fake orchestrator" stands in for
      the LLM, and geo_query is answered by a tiny in-memory fake gazetteer
      instead of a real Postgres/geo-mcp connection. Nothing external is
      required -- this runs anywhere, including this sandbox, right now.
      The point is to make the *shape* of the multi-tool decision loop
      concrete and inspectable, using the same function names as the real
      pipeline (detect/extract_entities/resolve/build_sql) so what you see
      here maps directly onto app/geo/detector.py, resolver.py, builder.py.

  --mode live (needs your real infrastructure -- run this on the RHEL host,
      inside/against the geo-mcp container, once you've validated the shape
      in mock mode)
      Uses the REAL geo_pipeline() from app/tools/geo_tools.py for the
      geo_query tool (so it actually resolves against your Postgres
      gazetteer), and a REAL Ollama /api/chat call (qwen2.5:32b) as the
      orchestrator deciding which tools to call and in what order.
      query_objects / create_graph are still local mocks, since those MCP
      servers don't exist yet -- but the orchestrator's tool-selection
      decisions and geo-mcp's resolution are both real.

Usage:
    python3 demo_orchestrator.py
    python3 demo_orchestrator.py "How did pothole reports near Highroad evolve over the last 6 months? Show me a graph."
    python3 demo_orchestrator.py --mode live "potholes near the traffic lights on highroad"
"""

import argparse
import difflib
import re
import sys
import textwrap

# --------------------------------------------------------------------------- #
# Pretty-printing helpers -- purely cosmetic, makes the trace readable
# --------------------------------------------------------------------------- #

WIDTH = 78


def rule(char: str = "=") -> None:
    print(char * WIDTH)


def header(text: str) -> None:
    rule()
    print(text)
    rule()


def say(role: str, text: str, indent: int = 0) -> None:
    prefix = " " * indent + f"[{role}] "
    wrapped = textwrap.fill(
        text, width=WIDTH, initial_indent=prefix, subsequent_indent=" " * len(prefix)
    )
    print(wrapped)


def blank() -> None:
    print()


# --------------------------------------------------------------------------- #
# MOCK mode: fake gazetteer standing in for the real urbis.gazetteer table,
# and a fake keyword-based orchestrator standing in for the real LLM.
# --------------------------------------------------------------------------- #

FAKE_GAZETTEER = {
    "highroad": {"id": "STREET:72707", "name": "Highroad", "score": 0.91},
    "rue de la loi": {"id": "STREET:1", "name": "Rue de la Loi", "score": 0.95},
    "avenue louise": {"id": "STREET:2", "name": "Avenue Louise", "score": 0.88},
    "anderlecht": {"id": "MUNICIPALITY:5000071", "name": "Anderlecht", "score": 0.93},
}

SPATIAL_TRIGGER_WORDS = {"near", "on", "at", "between", "from", "until", "in", "around"}
EVOLUTION_TRIGGER_WORDS = {"evolve", "evolved", "evolution", "trend", "history", "over time", "last"}
GRAPH_TRIGGER_WORDS = {"graph", "chart", "plot", "visuali", "show me"}


def mock_detect(prompt: str) -> dict:
    """Mirrors app/geo/detector.py::detect() -- cheap first pass."""
    lower = prompt.lower()
    has_spatial = any(w in lower for w in SPATIAL_TRIGGER_WORDS) or any(
        name in lower for name in FAKE_GAZETTEER
    )
    relationship = "NEAR"
    if "between" in lower:
        relationship = "BETWEEN"
    elif re.search(r"\bon\b", lower):
        relationship = "ON"
    return {"has_spatial": has_spatial, "relationship": relationship}


def mock_extract_entities(prompt: str, relationship: str) -> dict:
    """Mirrors app/geo/detector.py::extract_entities()."""
    lower = prompt.lower()
    entities = []
    for name in FAKE_GAZETTEER:
        if name in lower:
            entities.append({"text": name, "type": "STREET", "role": "anchor"})
    return {"entities": entities, "relationship": relationship, "distance_meters": None}


def mock_resolve(text: str) -> list:
    """Mirrors app/geo/resolver.py::resolve() -- fuzzy match against the fake gazetteer."""
    text = text.lower()
    if text in FAKE_GAZETTEER:
        return [FAKE_GAZETTEER[text]]
    close = difflib.get_close_matches(text, FAKE_GAZETTEER.keys(), n=1, cutoff=0.6)
    return [FAKE_GAZETTEER[close[0]]] if close else []


def mock_build_sql(table: str, relationship: str, gazetteer_id: str) -> str:
    """Mirrors app/geo/builder.py::build_sql() -- simplified, no real geometry available."""
    return (
        f"SELECT t.* FROM {table} t "
        f"WHERE ST_DWithin(t.geom, (SELECT geom FROM urbis.gazetteer WHERE id = '{gazetteer_id}'), 50)"
    )


def mock_geo_query(prompt: str, table: str) -> dict:
    """Stands in for a tools/call to geo-mcp's geo_query tool."""
    say("geo-mcp", "detect(prompt) -- deciding whether this prompt has spatial context")
    intent = mock_detect(prompt)
    say("geo-mcp", f"-> {intent}", indent=2)

    if not intent["has_spatial"]:
        say("geo-mcp", "no spatial context found, returning unfiltered query")
        return {"status": "ok", "sql": f"SELECT * FROM {table}", "entities": []}

    say("geo-mcp", "extract_entities(prompt) -- pulling out place references")
    extraction = mock_extract_entities(prompt, intent["relationship"])
    say("geo-mcp", f"-> {extraction}", indent=2)

    resolved = []
    for entity in extraction["entities"]:
        say("geo-mcp", f"resolve({entity['text']!r}) -- fuzzy match against the gazetteer")
        candidates = mock_resolve(entity["text"])
        say("geo-mcp", f"-> {candidates}", indent=2)
        if candidates:
            resolved.append(candidates[0])

    if not resolved:
        say("geo-mcp", "nothing resolved, falling back to unfiltered query")
        return {"status": "ok", "sql": f"SELECT * FROM {table}", "entities": []}

    sql = mock_build_sql(table, extraction["relationship"], resolved[0]["id"])
    say("geo-mcp", "build_sql(...) -- turning the relationship + geometry into SQL")
    say("geo-mcp", f"-> {sql}", indent=2)

    return {"status": "ok", "sql": sql, "entities": resolved, "relationship": extraction["relationship"]}


def mock_query_objects(sql_filter: str) -> dict:
    """Stands in for a tools/call to a future db-mcp's query_objects tool."""
    say("db-mcp (MOCK)", "this MCP server doesn't exist yet -- returning synthetic sample data")
    months = ["Feb", "Mar", "Apr", "May", "Jun", "Jul"]
    counts = [3, 5, 4, 7, 6, 9]
    return {"months": months, "counts": counts, "filter_used": sql_filter}


def mock_create_graph(months: list, counts: list, title: str) -> dict:
    """Stands in for a tools/call to a future chart-mcp's create_graph tool. Renders real ASCII output."""
    say("chart-mcp (MOCK)", "this MCP server doesn't exist yet -- rendering a real ASCII bar chart from the data")
    blank()
    print(f"   {title}")
    max_count = max(counts) if counts else 1
    for month, count in zip(months, counts):
        bar = "\u2588" * count
        print(f"   {month:4s}| {bar:<{max_count}} {count}")
    blank()
    return {"chart_type": "bar", "rendered": True}


# --------------------------------------------------------------------------- #
# MOCK orchestrator: deterministic keyword rules standing in for the LLM's
# tool-selection reasoning. This is NOT how the real system decides --
# app/geo/detector.py and the orchestrator LLM (section 13 of the technical
# reference doc) do this with actual language understanding. The point here
# is purely to make the *shape* of the multi-step decision loop visible.
# --------------------------------------------------------------------------- #

def run_mock(prompt: str, table: str = "potholes") -> None:
    header(f'USER PROMPT: "{prompt}"')
    blank()

    say(
        "orchestrator",
        "Available tools: geo_query (geo-mcp, real pipeline shape), "
        "query_objects (db-mcp, MOCK), create_graph (chart-mcp, MOCK)",
    )
    blank()

    lower = prompt.lower()
    needs_geo = any(name in lower for name in FAKE_GAZETTEER) or any(w in lower for w in SPATIAL_TRIGGER_WORDS)
    needs_history = any(w in lower for w in EVOLUTION_TRIGGER_WORDS)
    needs_graph = any(w in lower for w in GRAPH_TRIGGER_WORDS)

    sql = f"SELECT * FROM {table}"
    geo_result = None
    data_result = None

    # --- Step 1: geo_query ---
    if needs_geo:
        say("orchestrator", 'Reasoning: prompt references a place -> resolve geometry first via geo_query.')
        blank()
        say("tool call", f'geo_query(user_query={prompt!r}, table={table!r})')
        geo_result = mock_geo_query(prompt, table)
        sql = geo_result["sql"]
        blank()
        say("tool result", f"status={geo_result['status']}, sql={sql!r}")
    else:
        say("orchestrator", "Reasoning: no place reference detected -> skipping geo_query.")
    blank()

    # --- Step 2: query_objects ---
    if needs_history:
        say(
            "orchestrator",
            'Reasoning: prompt asks about change "over time" -> need historical records -> '
            "call query_objects, passing the geometry filter from step 1 directly "
            "(NOT re-typed by the model -- see the handoff note below).",
        )
        blank()
        say("tool call", f"query_objects(sql_filter={sql!r})")
        data_result = mock_query_objects(sql)
        blank()
        say("tool result", f"{data_result}")
    else:
        say("orchestrator", 'Reasoning: no "over time" / trend language detected -> skipping query_objects.')
    blank()

    # --- Step 3: create_graph ---
    if needs_graph and data_result:
        say("orchestrator", 'Reasoning: prompt explicitly asks to "show a graph" -> call create_graph on the retrieved series.')
        blank()
        say("tool call", f"create_graph(x={data_result['months']}, y={data_result['counts']}, title='Potholes near Highroad')")
        graph_result = mock_create_graph(data_result["months"], data_result["counts"], "Potholes near Highroad")
        say("tool result", f"{graph_result}")
    elif needs_graph:
        say("orchestrator", "Reasoning: a graph was requested but there's no data to plot yet -> skipping create_graph.")
    else:
        say("orchestrator", "Reasoning: no graph/chart language detected -> skipping create_graph.")
    blank()

    # --- Final answer ---
    header("FINAL ANSWER (composed by the orchestrator from the tool results above)")
    if data_result:
        trend = "risen" if data_result["counts"][-1] > data_result["counts"][0] else "fallen"
        print(
            textwrap.fill(
                f"Pothole reports near Highroad have {trend} from {data_result['counts'][0]} "
                f"in {data_result['months'][0]} to {data_result['counts'][-1]} in {data_result['months'][-1]}. "
                + ("See the chart above." if needs_graph else ""),
                width=WIDTH,
            )
        )
    elif geo_result:
        print(textwrap.fill(f"Here is the filtered query: {sql}", width=WIDTH))
    else:
        print(textwrap.fill("(No spatial or historical context detected -- would answer generically.)", width=WIDTH))
    rule()


# --------------------------------------------------------------------------- #
# LIVE mode: real geo_pipeline() + real Ollama orchestrator. Only works when
# run with PYTHONPATH=/app inside/against your actual geo-mcp deployment.
# query_objects / create_graph remain local mocks -- those MCP servers don't
# exist yet, but this exercises the REAL geo resolution and REAL LLM
# tool-selection reasoning end to end.
# --------------------------------------------------------------------------- #

def run_live(prompt: str, table: str = "potholes") -> None:
    try:
        import json

        import requests

        from app.config import ACTIVE_LLM_BACKEND, LLM_BACKENDS
        from app.tools.geo_tools import geo_pipeline
    except ImportError as exc:
        print(
            "Live mode needs to run where app/ is importable (PYTHONPATH=/app) "
            "and your .env-configured DB/LLM are reachable, e.g.:\n\n"
            "  podman exec geo-mcp python -m examples.demo_orchestrator --mode live \"...\"\n\n"
            f"Import error: {exc}"
        )
        sys.exit(1)

    header(f'USER PROMPT: "{prompt}"')
    blank()

    backend = LLM_BACKENDS[ACTIVE_LLM_BACKEND]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "geo_query",
                "description": "Resolve spatial context in a question into a SQL filter against an application table.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_query": {"type": "string"},
                        "table": {"type": "string"},
                    },
                    "required": ["user_query", "table"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_objects",
                "description": "(MOCK -- db-mcp not built yet) Fetch historical monthly counts of records matching a SQL filter.",
                "parameters": {
                    "type": "object",
                    "properties": {"sql_filter": {"type": "string"}},
                    "required": ["sql_filter"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_graph",
                "description": "(MOCK -- chart-mcp not built yet) Render a bar chart from a labelled data series.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "months": {"type": "array", "items": {"type": "string"}},
                        "counts": {"type": "array", "items": {"type": "integer"}},
                        "title": {"type": "string"},
                    },
                    "required": ["months", "counts", "title"],
                },
            },
        },
    ]

    messages = [
        {
            "role": "system",
            "content": (
                "You are an assistant with access to geo_query (resolve place references "
                "to a SQL filter), query_objects (fetch historical data matching a filter), "
                "and create_graph (visualize a data series). Use geo_query first if the "
                "question mentions a place. Use query_objects if it asks about change over "
                "time. Use create_graph if it asks for a chart/graph/plot."
            ),
        },
        {"role": "user", "content": prompt},
    ]

    for step in range(4):
        say("orchestrator", f"calling {backend['model']} with {len(tools)} tool(s) available...")
        resp = requests.post(
            f"{backend['base_url']}/api/chat",
            json={"model": backend["model"], "messages": messages, "tools": tools, "stream": False},
            timeout=60,
        )
        resp.raise_for_status()
        message = resp.json()["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            header("FINAL ANSWER")
            print(textwrap.fill(message.get("content", ""), width=WIDTH))
            return

        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            say("tool call", f"{name}({args})")

            if name == "geo_query":
                result = geo_pipeline(args["user_query"], args.get("table", table))
            elif name == "query_objects":
                result = mock_query_objects(args.get("sql_filter", ""))
            elif name == "create_graph":
                result = mock_create_graph(args.get("months", []), args.get("counts", []), args.get("title", ""))
            else:
                result = {"error": f"unknown tool {name}"}

            say("tool result", f"{result}")
            blank()
            messages.append({"role": "tool", "content": json.dumps(result, default=str)})

    print("(stopped after 4 tool-calling rounds)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="Prompt to run through the orchestrator")
    parser.add_argument("--mode", choices=["mock", "live"], default="mock")
    parser.add_argument("--table", default="potholes")
    args = parser.parse_args()

    prompt = args.prompt or "How did pothole reports near Highroad evolve over the last 6 months? Show me a graph."

    if args.mode == "mock":
        run_mock(prompt, table=args.table)
    else:
        run_live(prompt, table=args.table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
