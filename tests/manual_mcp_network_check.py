"""
tests/manual_mcp_network_check.py
===================================
NOT a pytest test -- this needs the real container running and a real
Postgres connection, so it doesn't belong in the automated smoke suite.
This is the one thing demo_orchestrator.py --mode live does NOT prove: it
calls geo_pipeline() in-process, bypassing the network entirely. This
script instead talks to the actual running container over streamable-http,
exactly the way the future Django orchestrator will.

Run from the HOST (outside any container), against the port you published
with `-p 8090:8090`:

    pip install mcp   # only needed on whatever machine runs this script
    python3 manual_mcp_network_check.py

Or point it at a different URL (e.g. container-to-container over geo-net,
from inside another container on that network):

    python3 manual_mcp_network_check.py http://geo-mcp:8090/mcp
"""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://localhost:8090/mcp"


async def main(url: str) -> None:
    print(f"Connecting to {url} ...")
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Session initialized OK.\n")

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"Tools advertised: {names}\n")
            expected = {"geo_query", "resolve_with_choice", "resolve_place", "resolve_address_tool", "ping"}
            missing = expected - set(names)
            if missing:
                print(f"!! MISSING TOOLS: {missing}")

            print("Calling ping() ...")
            ping_result = await session.call_tool("ping", {})
            print(f"  -> {ping_result.content}\n")

            print("Calling geo_query() with a real sample prompt ...")
            geo_result = await session.call_tool(
                "geo_query",
                {
                    "user_query": "potholes near highroad",  # from examples/sample_prompts.csv, id 1
                    "table": "potholes",
                },
            )
            print(f"  -> {geo_result.content}\n")

            print("If the geo_query call above returned a real status")
            print("(ok / needs_confirmation / unresolved_entity) rather than")
            print("an error, the container is genuinely working end to end:")
            print("network, MCP protocol, and the Postgres connection to pg17.")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(main(url))
