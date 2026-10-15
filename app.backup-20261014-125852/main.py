"""
app/main.py
===========
FastMCP server entrypoint. Exposes:
  - geo_query           : full pipeline (detect -> resolve -> build SQL),
                          with autonomy-level confirmation control and
                          optional project-scoped gazetteer extension
  - resolve_with_choice   : second call after a disambiguation prompt
  - resolve_place         : direct gazetteer lookup, bypassing the LLM
                            detector -- useful for testing the resolver
  - resolve_address_tool  : direct address lookup
  - geometries_to_geojson : batch WKT (working SRID) -> GeoJSON (EPSG:4326),
                            for callers that want to display resolved
                            geometries on a map (e.g. chat-platform's
                            agents/ orchestrator). Thin wrapper around
                            app/geo/mapviz.py's wkts_to_geojson(), which
                            app/tools/revision.py already relies on for its
                            own reasoning-map reports -- reused here rather
                            than reimplemented, since it's the one place
                            that does the EPSG:3812->4326 ST_Transform
                            correctly via PostGIS.
  - ping                  : liveness check

Logs to /srv/logs/geo-mcp/ on the host filesystem (bind-mount this path
into the container -- see the technical reference document, section 12).
"""

from mcp.server.fastmcp import FastMCP

from app.config import DEFAULT_AUTONOMY_LEVEL, SRID
from app.geo.mapviz import wkts_to_geojson
from app.geo.resolver import resolve, resolve_address
from app.logging_setup import setup_logging
from app.tools.geo_tools import geo_pipeline, resolve_with_choice as _resolve_with_choice

setup_logging()

mcp = FastMCP("geo-mcp", host="0.0.0.0", port=8090)


@mcp.tool()
def geo_query(
    user_query: str,
    table: str,
    geom_column: str = "geom",
    srid: int = SRID,
    project: str = "",
    autonomy_level: int = DEFAULT_AUTONOMY_LEVEL,
    buffer_meters: float = 0,
) -> dict:
    """
    Detect spatial context in a natural-language question and translate it
    into a SQL query against `table`.

    geom_column / srid: the target table's actual geometry column name and
      SRID (defaults: "geom", 3812) -- set these to match your application
      schema; they are not assumed to be UrbIS's.
    project: optional name of a registered project (app/config.py PROJECTS)
      whose extra named locations (e.g. an asset database's own points)
      should be searched alongside the UrbIS gazetteer for this call.
    autonomy_level: 1 = always ask for confirmation before running anything,
      2 (default) = only ask when a match is ambiguous/low-confidence or a
      distance had to be assumed, 3 = never ask, always return best-effort SQL.
    buffer_meters: override the NEAR/ON default buffer distance; leave 0 to
      use the prompt's stated distance if any, else the configured default.

    Returned "status" is one of:
      "ok"                : sql is ready to run as-is.
      "needs_confirmation": sql is proposed but not yet confirmed -- see
        entities/ambiguous_entities, or call resolve_with_choice once the
        end user has picked.
      "unresolved_entity" : at least one extracted entity (see
        unresolved_entities) had NO gazetteer match at all -- sql is null.
        This is deliberately distinct from "needs_confirmation": there is
        nothing to confirm, since no candidate geometry exists to build
        SQL against for that entity. The caller should tell the end user
        the place couldn't be found rather than running anything.
    """
    return geo_pipeline(
        user_query, table,
        geom_column=geom_column, srid=srid,
        project=project or None,
        autonomy_level=autonomy_level,
        buffer_meters=buffer_meters or None,
    )


@mcp.tool()
def resolve_with_choice(
    table: str,
    relationship: str,
    gazetteer_ids: list[str],
    geom_column: str = "geom",
    srid: int = SRID,
    buffer_meters: float = 0,
) -> dict:
    """
    Second step of the disambiguation flow: after geo_query returns
    ambiguous_entities with candidate gazetteer ids and the end user has
    picked one, call this with the chosen id(s) (in the same start/end/via
    order as the original entities) to get the final SQL without
    re-running LLM detection.
    """
    return _resolve_with_choice(
        table, relationship, gazetteer_ids,
        geom_column=geom_column, srid=srid, buffer_meters=buffer_meters or None,
    )


@mcp.tool()
def resolve_place(text: str, entity_type: str = "ANY", project: str = "") -> dict:
    """
    Directly resolve a place name against the gazetteer (streets, POIs,
    municipalities, statistical sectors, monitoring districts, region, and
    any project-specific extra sources), bypassing the LLM detector.
    """
    return {"candidates": resolve(text, entity_type=entity_type, project=project or None)}


@mcp.tool()
def resolve_address_tool(street_text: str, house_number: str = "", municipality_text: str = "") -> dict:
    """Directly resolve an address (street name + optional house number/municipality)."""
    return {
        "candidates": resolve_address(
            street_text,
            house_number=house_number or None,
            municipality_text=municipality_text or None,
        )
    }


@mcp.tool()
def ping() -> dict:
    return {"status": "ok"}


@mcp.tool()
def geometries_to_geojson(wkts: list[str], srid: int = SRID) -> dict:
    """
    Batch-converts WKT geometries (in the given working SRID, default the
    server's own SRID) to GeoJSON geometries in EPSG:4326, for callers
    that want to render resolved geometries on a standard web map. Returns
    one GeoJSON geometry (or null) per input WKT, in the same order --
    null for any that failed to parse/transform, so callers can align
    results back to their own entity list positionally.
    """
    return {"geojson": wkts_to_geojson(wkts, srid)}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
