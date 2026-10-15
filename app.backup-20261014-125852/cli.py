"""
app/cli.py
==========
Command-line entrypoint for manually testing the geo pipeline with a plain
prompt, without going through the MCP/JSON-RPC transport. Meant to be run
inside the running geo-mcp container via `podman exec`:

    podman exec geo-mcp python -m app.cli "potholes near the traffic lights on highroad"

    podman exec geo-mcp python -m app.cli \\
        "potholes near the traffic lights on highroad" \\
        --table potholes --geom-column geom --autonomy-level 3

    # Isolate just the resolver (no LLM call, straight gazetteer lookup):
    podman exec geo-mcp python -m app.cli --resolve-only "highroad" --entity-type STREET
"""

from __future__ import annotations

import argparse
import json

from app.config import DEFAULT_AUTONOMY_LEVEL, SRID
from app.geo.resolver import resolve, resolve_address
from app.tools.geo_tools import geo_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", help="Natural-language question, or the place text when --resolve-only is used")
    parser.add_argument("--table", default="potholes", help="Target application table (default: potholes)")
    parser.add_argument("--geom-column", default="geom")
    parser.add_argument("--srid", type=int, default=SRID)
    parser.add_argument("--project", default=None, help="Registered project name (see app/config.py PROJECTS)")
    parser.add_argument("--autonomy-level", type=int, default=DEFAULT_AUTONOMY_LEVEL, choices=[1, 2, 3])
    parser.add_argument("--buffer-meters", type=float, default=None)

    parser.add_argument("--resolve-only", action="store_true",
                         help="Skip the LLM detector entirely; just fuzzy-match `prompt` against the gazetteer")
    parser.add_argument("--entity-type", default="ANY", help="Used with --resolve-only")
    parser.add_argument("--address", action="store_true",
                         help="Used with --resolve-only: resolve as an address instead of a gazetteer entity")
    parser.add_argument("--house-number", default=None, help="Used with --resolve-only --address")

    args = parser.parse_args()

    if args.resolve_only:
        if args.address:
            result = {"candidates": resolve_address(args.prompt, house_number=args.house_number)}
        else:
            result = {"candidates": resolve(args.prompt, entity_type=args.entity_type, project=args.project)}
    else:
        result = geo_pipeline(
            args.prompt, args.table,
            geom_column=args.geom_column, srid=args.srid,
            project=args.project, autonomy_level=args.autonomy_level,
            buffer_meters=args.buffer_meters,
        )

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
