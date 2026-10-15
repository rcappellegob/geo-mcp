"""
app/config.py
=============
Central configuration: database connection (read from .env, same convention
as sync_urbis.py), the multi-backend LLM/embedding registry, and spatial
defaults used by the geo-detector/resolver/builder pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# .env loading (mirrors sync_urbis.py's behaviour)
# --------------------------------------------------------------------------- #

DEFAULT_ENV_FILE = Path(os.environ.get("GEO_MCP_ENV_FILE", "/srv/geo-mcp/.env"))


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    try:
        from dotenv import dotenv_values

        values = dotenv_values(str(env_path))
    except ImportError:
        values = {}
        for raw_line in env_path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    for key, val in values.items():
        if val is not None and key not in os.environ:
            os.environ[key] = val


_load_env_file(DEFAULT_ENV_FILE)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = int(os.environ.get("PGPORT", "5432"))
PGUSER = os.environ.get("PGUSER", "postgres")
PGPASSWORD = os.environ.get("PGPASSWORD")
PGDATABASE = os.environ.get("PGDATABASE", "mcp_db")
PGSCHEMA = os.environ.get("PGSCHEMA", "urbis")


def db_dsn() -> str:
    parts = [f"host={PGHOST}", f"port={PGPORT}", f"user={PGUSER}", f"dbname={PGDATABASE}"]
    if PGPASSWORD:
        parts.append(f"password={PGPASSWORD}")
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# LLM / embedding backends
#
# All backends are exposed through a single nginx host, path-routed to
# individual Ollama-compatible instances. Every {base_url} below is expected
# to answer the standard Ollama HTTP API:
#   POST {base_url}/api/generate   (chat/completion, supports "format": "json")
#   POST {base_url}/api/embeddings (embedding vectors)
# If your nginx routes differ from this (e.g. it already strips /api/*, or
# uses an OpenAI-compatible /v1/chat/completions path instead), adjust
# llm.py's request URLs accordingly -- this is flagged as an open question
# in the technical reference document.
# --------------------------------------------------------------------------- #

_LLM_NGINX_BASE = "http://5.9.23.184:8080"

LLM_BACKENDS = {
    "llama": {"base_url": f"{_LLM_NGINX_BASE}/llama", "model": "llama3:8b"},
    "gemma": {"base_url": f"{_LLM_NGINX_BASE}/gemma", "model": "gemma3:27b"},
    "qwen": {"base_url": f"{_LLM_NGINX_BASE}/qwen", "model": "qwen2.5:32b"},
    "mistral": {"base_url": f"{_LLM_NGINX_BASE}/mistral", "model": "mistral:7b"},
    "nemo": {"base_url": f"{_LLM_NGINX_BASE}/nemo", "model": "mistral-nemo"},
    "nemotron-super": {
        "base_url": f"{_LLM_NGINX_BASE}/nemotron-super",
        "model": "MHKetbi/nvidia_Llama-3.3-Nemotron-Super-49B-v1:q4_K_L",
    },
}

EMBEDDING_BACKENDS = {
    "all-minilm": {"base_url": f"{_LLM_NGINX_BASE}/embed", "model": "all-minilm"},
    "bge-m3": {"base_url": f"{_LLM_NGINX_BASE}/embed", "model": "bge-m3"},
    "mxbai-embed-large": {"base_url": f"{_LLM_NGINX_BASE}/embed", "model": "mxbai-embed-large"},
    "nomic-embed-text": {"base_url": f"{_LLM_NGINX_BASE}/embed", "model": "nomic-embed-text"},
    "snowflake-arctic-embed": {"base_url": f"{_LLM_NGINX_BASE}/embed", "model": "snowflake-arctic-embed"},
}

# Pick which backend to use for structured extraction (needs reliable JSON
# output + decent multilingual FR/NL understanding) and for embeddings.
# qwen2.5:32b is a reasonable default for structured JSON extraction;
# bge-m3 is a strong multilingual embedding model (FR/NL/EN) -- confirm both
# choices against your actual latency/quality constraints.
ACTIVE_LLM_BACKEND = os.environ.get("GEO_MCP_LLM_BACKEND", "qwen")
ACTIVE_EMBEDDING_BACKEND = os.environ.get("GEO_MCP_EMBEDDING_BACKEND", "bge-m3")

# Embedding vector dimension must match sql/lookup_views.sql's
# gazetteer_embeddings.embedding column -- keep these in sync.
EMBEDDING_DIMENSIONS = {
    "all-minilm": 384,
    "bge-m3": 1024,
    "mxbai-embed-large": 1024,
    "nomic-embed-text": 768,
    "snowflake-arctic-embed": 1024,
}

# --------------------------------------------------------------------------- #
# Spatial defaults
# --------------------------------------------------------------------------- #

SRID = 3812
DEFAULT_BUFFER_METERS = 50          # "near" / fallback radius
ON_BUFFER_METERS = 15               # "on" (e.g. "on Highroad") -- tighter
TRIGRAM_MIN_SIMILARITY = 0.3        # below this, fall back to embeddings if available
WORD_SIMILARITY_MIN = 0.5           # word_similarity() floor for shortened/partial names
EMBEDDING_MIN_SIMILARITY = 0.75     # cosine similarity floor to accept an embedding match
MAX_CANDIDATES = 3                  # candidates returned for disambiguation
# Two candidates are considered "too close to call" when the runner-up's
# score is within this fraction of the top score -- triggers disambiguation.
DISAMBIGUATION_MARGIN = 0.9

# When a resolved name matches the query text as a whole word (not just a
# trigram-similar substring -- e.g. "meiser" as a whole word in "Place
# Meiser" vs. buried inside the compound "Reyers-Meiser"), add this to its
# score before ranking/tie-breaking. See revision note 7.txt: "meiser" was
# resolving to "Tunnel Reyers-Meiser" over "Place Meiser" on a tied
# trigram score with no other signal to prefer the whole-word match.
EXACT_WORD_MATCH_BONUS = 0.15

# Bonus added to a candidate's score when its gazetteer type matches the
# entity type the LLM extractor guessed. A *bonus*, not a filter: "Meiser"
# got tagged MUNICIPALITY by the extractor (it's actually type STREET --
# revision note 7.txt) and a hard `type = 'MUNICIPALITY'` filter meant
# there was nothing to find at all, even though the real "Meiser" row
# existed right there in the gazetteer under a different type. Confirmed
# the same failure mode as the POI/TRANSIT_STOP case (Chien Vert) but for
# an entirely different pair of types -- not a two-type special case,
# a general problem with treating the LLM's type guess as ground truth.
# Search now always considers every type; this bonus just breaks ties in
# favour of the guessed type when candidates are otherwise close.
TYPE_MATCH_BONUS = 0.15

# Small score penalty for a candidate whose French name starts with
# "Clos" -- an unambiguous French urbanism term for a dead-end/cul-de-sac
# court, not a through street. Mentioned twice now across two rounds of
# revision notes ("better to take a road than a clos" / a routing query
# landing on "Clos Théodore De Cuyper" instead of a real through street)
# as a real, recurring wrong-answer pattern for BETWEEN/FROM_TO
# destinations. This is a *penalty*, not an exclusion -- if "Clos X" is
# genuinely the only or best match (or the user explicitly asked for a
# clos by name), it still wins; this only breaks a close tie in favour of
# a through street/avenue/etc. with a similar name. No reliable Dutch
# equivalent term is used here (candidates considered and rejected:
# "Hof" is also used for unrelated historical names, not just
# dead-ends, so applying the same penalty to it risked false positives).
CLOS_PENALTY = 0.2

# Gazetteer types that exist only as CONTEXT (reference roads and municipalities in the 5 km
# belt around the Region, see sync_urbis.py). They are searchable, but this penalty is
# subtracted from their score so a street/municipality inside the Region always wins a tie.
CONTEXT_TYPES = ("OUTSIDE_STREET", "NEIGHBOUR_MUNICIPALITY")
CONTEXT_PENALTY = 0.25

# -------------------------------------------------------------------- #
# Road classification fallback (revision notes 1.txt / 8.txt): terms like
# "highroad"/"highway" or "regional road" are not a named place -- they
# describe a *class* of road (StreetHierarchyDomain or
# StreetAdministratorDomain). app/geo/resolver.py::resolve_road_class()
# tries urbis.road_class_gazetteer (sql/lookup_views.sql section 3b) as a
# fallback, but the domain tables' own text ("Highway", "Régional",
# "Gewest") won't catch every phrasing a user types -- this maps common
# English/FR/NL variants people actually type onto a
# urbis.road_class_gazetteer id prefix ("HIERARCHY:" or "ADMIN:") so the
# fallback can short-circuit straight to the right row(s) instead of
# relying on trigram similarity against "Highway" or "Régional" alone.
# Not exhaustive -- extend as real misses turn up, same spirit as
# resolver.py's ABBREVIATIONS.
# -------------------------------------------------------------------- #
ROAD_CLASS_SYNONYMS: dict[str, list[str]] = {
    "highway": ["HIERARCHY:H"],
    "highroad": ["HIERARCHY:H"],
    "motorway": ["HIERARCHY:H"],
    "autoroute": ["HIERARCHY:H"],
    "snelweg": ["HIERARCHY:H"],
    "metropolitan road": ["HIERARCHY:MER"],
    "main road": ["HIERARCHY:MAR"],
    "inter-district road": ["HIERARCHY:IDR"],
    "interdistrict road": ["HIERARCHY:IDR"],
    "district collector": ["HIERARCHY:DC"],
    "district road": ["HIERARCHY:DR"],
    "trail": ["HIERARCHY:T"],
    "regional road": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "regional roads": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "region road": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "regional roads in brussels": ["ADMIN:REGION_RBC"],
    "route regionale": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "routes regionales": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "voirie regionale": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "voiries regionales": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "gewestweg": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "gewestwegen": ["ADMIN:REGION_RBC", "ADMIN:REGION_VL"],
    "highroads": ["HIERARCHY:H"],
    "highways": ["HIERARCHY:H"],
    "autoroutes": ["HIERARCHY:H"],
    "main roads": ["HIERARCHY:MAR"],
    "communal road": ["ADMIN:MUN_RBC", "ADMIN:MUN_VL"],
    "municipal road": ["ADMIN:MUN_RBC", "ADMIN:MUN_VL"],
}

# -------------------------------------------------------------------- #
# BETWEEN / FROM_TO corridor heuristic (revision notes 5.txt / 6.txt):
# per your decision, this stays a heuristic rather than full StreetAxes
# shortest-path routing -- app/geo/builder.py buffers a straight line
# between the two anchor centroids by this many metres and selects real
# StreetSurfaces polygons intersecting that buffered line, rather than
# using the (usually much wider) convex hull of the two anchor polygons.
# This still can't find a genuinely indirect route (e.g. the inner ring
# road case in 5.txt, where the real connecting road curves well away
# from the straight line between the two anchors) -- real StreetAxes/
# pgRouting routing has since been added as the first choice (see
# builder.py's _route_corridor_expr); this stays as the fallback for when
# that can't find a path. Also used as the default buffer for the real
# routing corridor itself. 25m rather than the earlier 30m default: two
# separate revision notes ("buffer of 25m", "clip... 25m from the road
# axes") both asked for 25m specifically; still fully overridable by an
# explicitly stated distance in the prompt either way.
# -------------------------------------------------------------------- #
ROUTE_CORRIDOR_BUFFER_METERS = 25

# --------------------------------------------------------------------------- #
# Autonomy levels -- how much geo-mcp confirms with the caller before
# committing to resolved parameters (entity matches, buffer distances,
# ambiguous relationships). See technical reference doc section 11.
#   LEVEL_1_CONFIRM_ALL : always return proposed parameters for confirmation,
#                         even a single unambiguous high-confidence match.
#   LEVEL_2_CONFIRM_RISKY: only ask when confidence is low, a match is
#                         ambiguous, or a default (e.g. buffer distance) had
#                         to be assumed rather than being stated explicitly.
#   LEVEL_3_AUTONOMOUS  : never ask; always run with the best available
#                         resolution and stated/default parameters.
# --------------------------------------------------------------------------- #

LEVEL_1_CONFIRM_ALL = 1
LEVEL_2_CONFIRM_RISKY = 2
LEVEL_3_AUTONOMOUS = 3

DEFAULT_AUTONOMY_LEVEL = int(os.environ.get("GEO_MCP_AUTONOMY_LEVEL", LEVEL_2_CONFIRM_RISKY))

# --------------------------------------------------------------------------- #
# Project registry -- lets a caller extend the gazetteer with its own
# application-specific named locations (e.g. an asset database's traffic
# lights, or project-specific zones) without touching the UrbIS-derived
# tables. Each project maps to extra SQL sources UNIONed into gazetteer
# search at query time, plus that project's own default target-table shape.
#
# Register new projects here (or load this dict from a small Postgres table
# / a project-specific .env if you'd rather not redeploy code to add one --
# flagged as a possible follow-up in the technical reference doc).
#
# extra_sources: list of dicts, each describing one extra unionable source:
#   {
#     "sql": "<SELECT id, type, name_fr, name_nl, name_fr_norm, name_nl_norm, geom, centroid FROM ...>",
#     "type": "TRAFFIC_LIGHT",       # entity type this source represents
#   }
# The SELECT must return exactly the same columns/order as urbis.gazetteer.
# --------------------------------------------------------------------------- #

PROJECTS: dict[str, dict] = {
    # Example only -- replace with your real asset-database source once its
    # schema is known:
    # "roadworks_app": {
    #     "extra_sources": [
    #         {
    #             "type": "TRAFFIC_LIGHT",
    #             "sql": """
    #                 SELECT
    #                     'TRAFFIC_LIGHT:' || id::text, 'TRAFFIC_LIGHT', id,
    #                     'assetdb.traffic_lights', name, name,
    #                     unaccent(lower(name)), unaccent(lower(name)),
    #                     geom, geom
    #                 FROM assetdb.traffic_lights
    #             """,
    #         },
    #     ],
    #     "default_geom_column": "geom",
    #     "default_srid": 3812,
    # },
}

# --------------------------------------------------------------------------- #
# Logging
#
# Per RHEL host convention: geo-mcp logs to the host filesystem at
# /srv/logs/geo-mcp/, NOT inside the podman container -- bind-mount this
# path (-v /srv/logs/geo-mcp:/srv/logs/geo-mcp:Z) so it survives container
# restarts and is directly readable from the host. See app/logging_setup.py.
# --------------------------------------------------------------------------- #

LOG_DIR = Path(os.environ.get("GEO_MCP_LOG_DIR", "/srv/logs/geo-mcp"))
LOG_FILE = LOG_DIR / "geo-mcp.log"
AUDIT_LOG_FILE = LOG_DIR / "geo-mcp-audit.log"


# =========================================================================== #
# Revision round 2: ranking, buffers and routing  (see app/geo/resolver.py / builder.py)
# =========================================================================== #
# An entity whose normalised name EQUALS the normalised prompt text beats one that merely contains it
# ("Chien Vert" the STIB stop > "Rue du Chien Vert"), unless a street word is in the prompt text.
EXACT_NAME_BONUS = 0.30
# The prompt text contains a street word (rue, avenue, place, square, rond-point, laan, straat ...):
# the user means a road, so STREET rows get this bonus (also over a POI/stop of the same name).
STREET_WORD_BONUS = 0.30
STREET_WORDS = (
    "rue", "avenue", "boulevard", "chaussee", "place", "square", "quai", "allee", "drive", "dreef", "impasse",
    "rond point", "rondpoint", "rotonde", "laan", "straat", "steenweg", "weg", "plein", "lei", "pont", "pad", "chemin", "clos",
)
# Tie-break priors per gazetteer type (added to every score). Small on purpose.
TYPE_PRIOR = {
    "STREET": 0.10, "MUNICIPALITY": 0.10, "TRANSIT_STOP": 0.05, "TRANSIT_LINE": 0.05, "RAILWAY": 0.05,
    "STATISTICAL_SECTOR": 0.0, "MONITORING_DISTRICT": -0.05, "POI": 0.0, "REGION": 0.0,
}
# Ranking hints derived from words in the prompt ("zone X", "quartier X", "arret X") -> {type: bonus}
HINT_ADJUST = {
    "AREA": {"MONITORING_DISTRICT": 0.35, "STATISTICAL_SECTOR": 0.30, "MUNICIPALITY": 0.30, "POI": -0.2, "TRANSIT_STOP": -0.2},
    "PLACE": {"STREET": 0.25, "MONITORING_DISTRICT": 0.0, "TRANSIT_STOP": -0.30, "POI": -0.30},
    "LINE": {"TRANSIT_LINE": 0.30, "RAILWAY": 0.30},
    "STOP": {"TRANSIT_STOP": 0.30},
}
# Spoken names for things the gazetteer knows under another name (normalised text -> gazetteer text)
ENTITY_ALIASES = {
    "ring": "r0", "la ring": "r0", "de ring": "r0", "grote ring": "r0", "ring de bruxelles": "r0",
    "ring van brussel": "r0", "boulevard peripherique": "r0", "brusselse ring": "r0", "brussels ring": "r0",
}
# Buffers (metres). An explicit distance in the prompt always wins over all of these.
LINEAR_BUFFER_METERS = 20          # tram line / railway / numbered belt road: "near / around / buffer"
SELF_POINT_BUFFER_METERS = 50      # a bare POI / stop / address ("the Stoclet palace"): small buffer
AREA_BUFFER_METERS = 0             # municipalities, sectors, districts: NO buffer
ROAD_TOLERANCE_METERS = 3          # assets are matched to a selected road surface within this distance
ROUTE_HALF_WIDTH_METERS = 40       # half width of the corridor that clips road surfaces along a route
