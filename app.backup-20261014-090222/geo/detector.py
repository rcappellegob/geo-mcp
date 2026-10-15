"""
app/geo/detector.py
====================
Step 1 of the pipeline: does this prompt carry a geospatial context at all,
and if so, which entities and which spatial relationship is it expressing?

Kept as two separate LLM calls (like the original blueprint) so "has_spatial"
can short-circuit cheaply for the many prompts that carry no location at all.
"""

from __future__ import annotations

from app.geo.parsing import repair_extraction
from app.llm import llm_json

# Entity types the resolver (app/geo/resolver.py) knows how to resolve,
# matching what's actually in the gazetteer (sql/lookup_views.sql).
ENTITY_TYPES = [
    "STREET",              # e.g. "Highroad", "Rue de la Loi"
    "POI",                 # e.g. "traffic lights", "the hospital" -- see note below
    "MUNICIPALITY",
    "STATISTICAL_SECTOR",
    "MONITORING_DISTRICT",
    "REGION",
    "ADDRESS",             # "12 Rue de la Loi", "number 12 on Highroad"
    "TRANSIT_STOP",        # a named STIB stop, e.g. "the Chien Vert stop", "tram stop at Meiser"
    "TRANSIT_LINE",        # a STIB line: "tram 8", "metro 1", "bus 71", "ligne de tram 8"
    "RAILWAY",             # the railway / a railway line: "spoorweg", "chemin de fer", "railway"
]

RELATIONSHIPS = ["NEAR", "ON", "WITHIN", "BETWEEN", "FROM_TO", "INTERSECTION", "NONE"]


def detect(user_query: str) -> dict:
    """Cheap first pass: does this query have any geospatial context?"""
    prompt = f"""
You are a geospatial intent classifier for questions asked against an urban
infrastructure database (streets, addresses, points of interest, municipal
boundaries) in Brussels, Belgium. Names may be in French or Dutch.

Return ONLY this JSON object, nothing else:
{{
  "has_spatial": true or false,
  "relationship": one of {RELATIONSHIPS}
}}

"has_spatial" is true if the question refers to a place, street, address,
neighbourhood, or spatial relationship (near, on, between, from ... to ...,
within, at the intersection of). It is false for questions with no location
reference at all.

Query: "{user_query}"
"""
    return llm_json(prompt)


def extract_entities(user_query: str) -> dict:
    """Second pass: pull out the actual place references and their roles."""
    prompt = f"""
Extract geospatial entities from this question about Brussels urban
infrastructure. Names may be French or Dutch and may be misspelled,
abbreviated, or shortened (e.g. "Louise" for "Avenue Louise").

Return ONLY this JSON object, nothing else:
{{
  "entities": [
    {{"text": "...", "type": one of {ENTITY_TYPES}, "role": "anchor|main|start|end|via"}}
  ],
  "relationship": one of {RELATIONSHIPS},
  "distance_meters": a number if the question states an explicit distance
    (e.g. "within 100m", "within 50 meters"), otherwise null
}}

Rules:
- "role" is "anchor" for a single reference point (e.g. NEAR/ON/WITHIN),
  "start"/"end" for BETWEEN or FROM_TO (two streets or points), and "via"
  for an intermediate point (e.g. "... until the church").
- A generic feature that is not itself a named place (e.g. "potholes",
  "traffic lights" used generically rather than as a specific POI name) is
  NOT a geospatial entity -- it is the thing being searched for, handled
  separately by the calling application. Only extract entities that can be
  looked up by name in a gazetteer of streets/addresses/POIs/municipalities.
- If unsure of a type, prefer STREET for road names and POI for anything else.
- A street name immediately followed by a house/building number (e.g.
  "Avenue Marcel Thiry 52", "12 Rue de la Loi", "Highroad number 52") is
  ONE single ADDRESS entity -- put the full "<street> <number>" (or
  "<number> <street>") text together in "text". Do NOT split it into a
  separate STREET entity plus a second entity for just the number; a bare
  number with no gazetteer lookup of its own is not useful on its own.
  Example: "from Avenue Marcel Thiry 52 to Rue Théodore De Cuyper" ->
  entities: [
    {{"text": "Avenue Marcel Thiry 52", "type": "ADDRESS", "role": "start"}},
    {{"text": "Rue Théodore De Cuyper", "type": "STREET", "role": "end"}}
  ]
  -- exactly two entities, not three.
- The same rule applies even when the street name and the house number
  are mentioned separately in the sentence rather than right next to each
  other -- combine them into ONE ADDRESS entity for that role and do NOT
  ALSO emit the bare street name as its own entity for the same role.
  Example: "road quality on Marcel Thiry between number 52 and Theodore
  De Cuyper" -> entities: [
    {{"text": "Marcel Thiry 52", "type": "ADDRESS", "role": "start"}},
    {{"text": "Theodore De Cuyper", "type": "STREET", "role": "end"}}
  ]
  -- NOT three entities with "Marcel Thiry" repeated as its own bare
  STREET entity alongside the address; the address already covers it.
- A phrase describing a *classification* of road rather than a specific
  road's own name (e.g. "regional roads", "the highway", "main roads")
  is still worth extracting as a STREET entity -- the resolver has a
  fallback specifically for this. Example: "pedestrian crossings on
  regional roads in Etterbeek" -> entities: [
    {{"text": "regional roads", "type": "STREET", "role": "anchor"}},
    {{"text": "Etterbeek", "type": "MUNICIPALITY", "role": "anchor"}}
  ]
- Only set distance_meters if the user stated a number explicitly -- do not
  guess or infer one from words like "near" alone.
- "X between Y and Z" (e.g. "Kasteellaan between Basilieklaan and Bènèslaan"):
  X is the road the question is about -> role "main"; Y is "start"; Z is "end".
  Without an X ("between Y and Z", "from Y to Z") there is no "main".
  "... via V" -> V has role "via".
- A bare place name with no relation ("Rue de la Loi", "the Grand Place", "Berchem")
  has relationship "NONE" and ONE entity with role "anchor" -- never invent NEAR.
- "buffer of 500m around X" / "within 100 m of X" -> relationship "NEAR" and distance_meters.
- Copy entity text EXACTLY as written in the question (same spelling, even if it
  looks misspelled) -- do not correct, translate or re-spell it.
- Words that only describe the kind of place ("zone", "quartier", "wijk") may be
  left out of "text". A railway word ("spoorweg", "chemin de fer") is a RAILWAY entity.

Query: "{user_query}"
"""
    result = llm_json(prompt)
    result["entities"] = _dedup_subsumed_entities(result.get("entities", []))
    # deterministic repair (house numbers, frames, typos, generic words, relationship) -- app/geo/parsing.py
    return repair_extraction(user_query, result)


def _dedup_subsumed_entities(entities: list[dict]) -> list[dict]:
    """
    Deterministic backstop for the "don't repeat the street name that's
    already inside an address" rule above -- prompt wording alone didn't
    reliably stop the LLM from also emitting the bare street name as its
    own entity (confirmed against real output: "Marcel Thiry" extracted
    separately from, and *before*, "Avenue Marcel Thiry 52" for the same
    start role -- so this can't just check each entity against ones seen
    earlier in the list, it has to compare every pair), which fed an
    extra, wrong anchor into BETWEEN/FROM_TO's routing chain.

    Drops any entity whose text is a strict substring of another entity's
    text for the same role, keeping the longer (more specific) one.
    Exact duplicates for the same role are also collapsed to one. Only
    compares within the same role -- a start and an end can legitimately
    share overlapping text (e.g. two different addresses on one street).
    """
    normalized = [
        (e, e.get("role", "anchor"), e.get("text", "").strip().lower())
        for e in entities
    ]
    kept: list[dict] = []
    seen_exact: set[tuple[str, str]] = set()
    for i, (entity, role, text) in enumerate(normalized):
        if (role, text) in seen_exact:
            continue  # exact duplicate for this role, already kept once
        subsumed = any(
            role == other_role and text != other_text and text in other_text
            for j, (_, other_role, other_text) in enumerate(normalized)
            if j != i
        )
        if subsumed:
            continue
        seen_exact.add((role, text))
        kept.append(entity)
    return kept
