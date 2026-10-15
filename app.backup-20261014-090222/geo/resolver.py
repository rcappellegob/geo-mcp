"""
app/geo/resolver.py
====================
Step 2 of the pipeline: turn an extracted entity ("highroad", "hopital
saint-pierre", "av. louise", "12 rue de la loi") into an actual geometry.

Matching strategy, in order:
  1. Trigram whole-string similarity (pg_trgm `similarity()`) AND
     word-similarity (pg_trgm `word_similarity()`, operator `<%`) against
     unaccented, lowercased FR and NL names. word_similarity is what makes
     shortened references work ("Louise" matching "Avenue Louise") -- plain
     similarity() compares whole strings and penalises length mismatches
     too heavily for that case. The best of the two scores is used.
  2. If the best score is below TRIGRAM_MIN_SIMILARITY / WORD_SIMILARITY_MIN
     and a gazetteer_embeddings table has been populated, fall back to
     pgvector cosine similarity for a semantic match.
  3. Returns up to MAX_CANDIDATES matches so the caller can disambiguate.

A light abbreviation-expansion pass runs on the query text first (common FR/
NL street-name abbreviations), since typed queries often use "av.", "bd",
"ch.", etc. This is a best-effort normalisation, not a full gazetteer of
abbreviations -- extend ABBREVIATIONS as real-world misses turn up.

project-scoped extensibility (see app.config.PROJECTS): when `project` is
given, the resolver additionally searches that project's extra_sources
(e.g. an asset database's own named locations, such as traffic lights not
present in UrbIS), UNIONed with the UrbIS gazetteer for this call only.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional

from app.config import (
    ACTIVE_EMBEDDING_BACKEND,
    CLOS_PENALTY,
    CONTEXT_PENALTY,
    CONTEXT_TYPES,
    EMBEDDING_MIN_SIMILARITY,
    ENTITY_ALIASES,
    EXACT_NAME_BONUS,
    EXACT_WORD_MATCH_BONUS,
    HINT_ADJUST,
    MAX_CANDIDATES,
    PROJECTS,
    ROAD_CLASS_SYNONYMS,
    STREET_WORD_BONUS,
    STREET_WORDS,
    TRIGRAM_MIN_SIMILARITY,
    TYPE_MATCH_BONUS,
    TYPE_PRIOR,
    WORD_SIMILARITY_MIN,
)
from app.db import query
from app.llm import embed

# Best-effort FR/NL street-name abbreviation expansion, applied to the query
# text only (stored gazetteer names are assumed to already be spelled out).
ABBREVIATIONS = {
    r"\bav\.?\b": "avenue",
    r"\bbd\.?\b": "boulevard",
    r"\bbld\.?\b": "boulevard",
    r"\bch\.?\b": "chaussee",
    r"\br\.?\b": "rue",
    r"\bpl\.?\b": "place",
    r"\bsq\.?\b": "square",
    r"\bimp\.?\b": "impasse",
    r"\bstr\.?\b": "straat",
    r"\bln\.?\b": "laan",
    r"\bst\.?\b": "sint",
}


def _expand_abbreviations(text: str) -> str:
    """lower-case, accent-free (the gazetteer *_norm columns are unaccented: 'Bènès' must meet 'benes'),
    abbreviations expanded, punctuation turned into single spaces."""
    result = unicodedata.normalize("NFKD", text or "")
    result = "".join(c for c in result if not unicodedata.combining(c)).lower()
    for pattern, expansion in ABBREVIATIONS.items():
        result = re.sub(pattern, expansion, result)
    return re.sub(r"[^a-z0-9]+", " ", result).strip()


_LEADING_ARTICLE = re.compile(r"^(?:le|la|les|l|het|de|den|the)\s+")


def _fold_needle(text: str) -> str:
    n = _expand_abbreviations(text)
    stripped = _LEADING_ARTICLE.sub("", n).strip()
    return stripped if len(stripped) >= 3 else n


def _has_street_word(needle: str) -> bool:
    """True if the (folded) text contains a street word: 'rue', 'avenue', 'place', 'rond point', 'laan', 'xxxstraat' ..."""
    padded = f" {needle} "
    if any(f" {w} " in padded for w in STREET_WORDS):
        return True
    return bool(re.search(r"(laan|straat|steenweg|plein|dreef|lei)\b", needle))


def _type_adjustments(needle: str, entity_type: Optional[str], hint: Optional[str], aliased: bool) -> dict:
    adj = dict(TYPE_PRIOR)
    if _has_street_word(needle):
        adj["STREET"] = adj.get("STREET", 0) + STREET_WORD_BONUS
    if hint:
        for t, v in HINT_ADJUST.get(hint, {}).items():
            adj[t] = adj.get(t, 0) + v
    elif entity_type and entity_type not in ("ANY", "POI"):
        # the LLM's "POI" is its default for "anything else" (see detector prompt) -> not evidence of a point of interest
        adj[entity_type] = adj.get(entity_type, 0) + TYPE_MATCH_BONUS
    if aliased:
        adj["OUTSIDE_STREET"] = adj.get("OUTSIDE_STREET", 0) + CONTEXT_PENALTY + 0.2
    return adj


def _collapse_aliases(rows: list[dict], limit: int) -> list[dict]:
    """Alias rows (id 'OSM_POI:way/1#ab12cd', 'TRANSIT_LINE:tram:8#2', 'RAILWAY:ALL#2') are extra
    searchable NAMES of one place. Keep the best-scoring row per place and report the place's
    canonical id (the part before '#'), so a place never takes two of the MAX_CANDIDATES slots."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        canon = str(r["id"]).split("#", 1)[0]
        if canon in seen:
            continue
        seen.add(canon)
        if canon != r["id"]:
            r = {**r, "id": canon, "matched_alias": r.get("name_fr")}
        out.append(r)
    return out[:limit]


def _gazetteer_source_sql(project: Optional[str]) -> str:
    """
    Build the FROM-able source for this call: just urbis.gazetteer, or that
    UNIONed with a project's extra_sources when a project is given.
    """
    sources = ["SELECT * FROM urbis.gazetteer"]
    if project:
        project_cfg = PROJECTS.get(project)
        if project_cfg is None:
            raise ValueError(f"Unknown project {project!r} (see app/config.py PROJECTS)")
        for extra in project_cfg.get("extra_sources", []):
            sources.append(extra["sql"])
    return " UNION ALL ".join(f"({s})" for s in sources)


def _trigram_candidates(text: str, entity_type: Optional[str], limit: int, project: Optional[str],
                        hint: Optional[str] = None, needle: Optional[str] = None, aliased: bool = False) -> list[dict]:
    needle = needle if needle is not None else _fold_needle(text)
    adj = _type_adjustments(needle, entity_type, hint, aliased)
    source = _gazetteer_source_sql(project)
    # score = best of the trigram similarities
    #       + whole-word bonus            ("meiser" as a word inside the name)
    #       + EXACT_NAME_BONUS            (the name EQUALS the text: "Chien Vert" the stop > "Rue du Chien Vert")
    #       + per-type adjustment `adj`   (type prior + street-word bonus + hint ("zone"/"quartier") + LLM type guess)
    #       - clos / context penalties
    sql = f"""
        SET pg_trgm.word_similarity_threshold = 0.4;
        WITH src AS ({source})
        SELECT
            id, type, name_fr, name_nl,
            ST_AsText(geom) AS wkt,
            ST_AsText(centroid) AS centroid_wkt,
            GREATEST(
                similarity(name_fr_norm, %(needle)s),
                similarity(name_nl_norm, %(needle)s),
                word_similarity(%(needle)s, name_fr_norm),
                word_similarity(%(needle)s, name_nl_norm)
            )
            + CASE WHEN
                %(needle)s = ANY(regexp_split_to_array(name_fr_norm, '[^a-z0-9]+'))
                OR %(needle)s = ANY(regexp_split_to_array(name_nl_norm, '[^a-z0-9]+'))
              THEN %(word_bonus)s ELSE 0 END
            + CASE WHEN btrim(regexp_replace(name_fr_norm, '[^a-z0-9]+', ' ', 'g')) = %(needle)s
                     OR btrim(regexp_replace(name_nl_norm, '[^a-z0-9]+', ' ', 'g')) = %(needle)s
              THEN %(exact_bonus)s ELSE 0 END
            + COALESCE((%(adj)s::jsonb ->> type)::float8, 0)
            - CASE WHEN name_fr_norm LIKE 'clos %%' THEN %(clos_penalty)s ELSE 0 END
            - CASE WHEN type = ANY(%(context_types)s) THEN %(context_penalty)s ELSE 0 END AS score
        FROM src
        WHERE (
            name_fr_norm %% %(needle)s OR name_nl_norm %% %(needle)s
            OR %(needle)s <%% name_fr_norm OR %(needle)s <%% name_nl_norm
        )
        ORDER BY score DESC
        LIMIT %(fetch)s
    """
    params = {
        "needle": needle,
        "adj": json.dumps(adj),
        "fetch": limit * 8,  # alias rows and duplicates are collapsed below, so fetch extra
        "word_bonus": EXACT_WORD_MATCH_BONUS,
        "exact_bonus": EXACT_NAME_BONUS,
        "clos_penalty": CLOS_PENALTY,
        "context_types": list(CONTEXT_TYPES),
        "context_penalty": CONTEXT_PENALTY,
    }
    return _dedupe(_collapse_aliases(query(sql, params), limit * 8), limit)


def _fold_name(c: dict) -> str:
    return _expand_abbreviations(c.get("name_fr") or c.get("name_nl") or "")


def _dedupe(cands: list[dict], limit: int) -> list[dict]:
    """The same real place shows up several times (5 POIs 'CHIEN VERT', an OSM relation that repeats a street
    name): keep the best row per (type, name) for point-like types, and drop a POI that only repeats the
    name of a STREET candidate."""
    cands = sorted(cands, key=lambda c: -c["score"])
    street_names = {_fold_name(c) for c in cands if c["type"] == "STREET"}
    seen, out = set(), []
    for c in cands:
        name = _fold_name(c)
        if c["type"] == "POI" and name in street_names:
            continue
        if c["type"] in ("POI", "TRANSIT_STOP", "MONITORING_DISTRICT", "STATISTICAL_SECTOR"):
            key = (c["type"], name)
            if key in seen:
                continue
            seen.add(key)
        out.append(c)
    return out[:limit]


def _embedding_candidates(text: str, entity_type: Optional[str], limit: int, project: Optional[str],
                          hint: Optional[str] = None) -> list[dict]:
    vec = embed(text, backend=ACTIVE_EMBEDDING_BACKEND)
    vec_literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
    apply_type_bonus = bool(entity_type) and entity_type != "ANY"
    source = _gazetteer_source_sql(project)
    sql = f"""
        WITH src AS ({source})
        SELECT
            g.id, g.type, g.name_fr, g.name_nl,
            ST_AsText(g.geom) AS wkt,
            ST_AsText(g.centroid) AS centroid_wkt,
            (1 - (e.embedding <=> %(vec)s::vector))
            + CASE WHEN %(apply_type_bonus)s AND g.type = %(entity_type)s
              THEN %(type_bonus)s ELSE 0 END
            - CASE WHEN g.type = ANY(%(context_types)s) THEN %(context_penalty)s ELSE 0 END AS score
        FROM urbis.gazetteer_embeddings e
        JOIN src g ON g.id = e.gazetteer_id
        ORDER BY score DESC
        LIMIT %(fetch)s
    """
    params = {
        "vec": vec_literal,
        "entity_type": entity_type,
        "apply_type_bonus": apply_type_bonus,
        "fetch": limit * 4,
        "type_bonus": TYPE_MATCH_BONUS,
        "context_types": list(CONTEXT_TYPES),
        "context_penalty": CONTEXT_PENALTY,
    }
    candidates = _collapse_aliases(query(sql, params), limit)
    return [c for c in candidates if c["score"] >= EMBEDDING_MIN_SIMILARITY]


def _road_class_trigram_candidates(text: str, limit: int) -> list[dict]:
    """Trigram fallback against urbis.road_class_gazetteer's own text
    (StreetHierarchyDomain's English hierarchyname, or
    StreetAdministratorDomain's FR/NL description) when no
    ROAD_CLASS_SYNONYMS entry matched directly."""
    needle = _expand_abbreviations(text)
    sql = """
        SET pg_trgm.word_similarity_threshold = 0.4;
        SELECT
            id, type, name_fr, name_nl,
            ST_AsText(geom) AS wkt,
            ST_AsText(centroid) AS centroid_wkt,
            GREATEST(
                similarity(name_fr_norm, %(needle)s),
                similarity(name_nl_norm, %(needle)s),
                word_similarity(%(needle)s, name_fr_norm),
                word_similarity(%(needle)s, name_nl_norm)
            ) AS score
        FROM urbis.road_class_gazetteer
        WHERE (
            name_fr_norm %% %(needle)s OR name_nl_norm %% %(needle)s
            OR %(needle)s <%% name_fr_norm OR %(needle)s <%% name_nl_norm
        )
        ORDER BY score DESC
        LIMIT %(limit)s
    """
    return query(sql, {"needle": needle, "limit": limit})


def resolve_road_class(text: str, limit: int = MAX_CANDIDATES) -> list[dict]:
    """
    Resolve a road-*classification* term ("highroad"/"highway", "regional
    roads", ...) to urbis.road_class_gazetteer row(s) -- revision notes
    1.txt / 8.txt. Not a named place lookup: the geometry returned is
    every StreetSurfaces segment of that class, merged citywide (or
    within whatever area another resolved entity narrows it to -- see
    app/tools/geo_tools.py's AND-together handling for e.g. "regional
    roads in Etterbeek").

    Tries ROAD_CLASS_SYNONYMS (app/config.py) first for an exact phrase
    hit -- this is what makes "highroad"/"highway"/"regional roads" work
    even though those exact words don't appear in the FR/NL domain-table
    text -- then falls back to trigram matching against the domain
    tables' own text (English hierarchy names, FR/NL admin descriptions)
    for phrasings not in the synonym list yet.
    """
    normalized = _expand_abbreviations(text).strip()
    ids = _road_class_exact(normalized)
    if ids:
        rows = query(
            """
            SELECT id, type, name_fr, name_nl,
                   ST_AsText(geom) AS wkt, ST_AsText(centroid) AS centroid_wkt
            FROM urbis.road_class_gazetteer
            WHERE id = ANY(%(ids)s)
            """,
            {"ids": ids},
        )
        for r in rows:
            r["score"] = 1.0  # direct synonym hit: treat as fully confident
        return rows[:limit]

    return _road_class_trigram_candidates(text, limit)


def _road_class_exact(norm: str) -> Optional[list[str]]:
    for key, ids in ROAD_CLASS_SYNONYMS.items():
        if _expand_abbreviations(key) == norm:
            return ids
    return None


def resolve(
    text: str,
    entity_type: str = "ANY",
    limit: int = MAX_CANDIDATES,
    project: Optional[str] = None,
    hint: Optional[str] = None,
) -> list[dict]:
    """
    Resolve free text to gazetteer candidates, best match first.

    Order (revision round 2):
      0. a spoken alias ("de ring" -> R0);
      1. a road-CLASSIFICATION phrase ("highroad", "regional roads", "gewestwegen") is answered from
         urbis.road_class_gazetteer straight away -- before, this only ran after name matching had
         failed, so "highroad" was captured by the POI "High Tech Ninja" at 0.44 (item 1);
      2. trigram name matching with the exact-name / street-word / hint ranking above;
      3. embedding fallback; 4. road-class trigram fallback.
    """
    norm = _fold_needle(text)
    aliased = False
    if norm in ENTITY_ALIASES:
        norm, aliased = ENTITY_ALIASES[norm], True

    ids = _road_class_exact(norm)
    if ids:
        try:
            rows = resolve_road_class(norm, limit)
        except Exception:
            rows = []
        if rows:
            return rows

    candidates = _trigram_candidates(text, entity_type, limit, project, hint=hint, needle=norm, aliased=aliased)
    threshold = min(TRIGRAM_MIN_SIMILARITY, WORD_SIMILARITY_MIN)
    if candidates and candidates[0]["score"] >= threshold:
        return candidates

    try:
        embedding_candidates = _embedding_candidates(text, entity_type, limit, project, hint=hint)
    except Exception:
        embedding_candidates = []
    if embedding_candidates:
        return embedding_candidates

    if entity_type in ("STREET", "ANY"):
        try:
            road_class_candidates = resolve_road_class(text, limit)
        except Exception:
            road_class_candidates = []
        if road_class_candidates:
            return road_class_candidates

    return candidates


def resolve_address(
    street_text: str,
    house_number: Optional[str] = None,
    municipality_text: Optional[str] = None,
    limit: int = MAX_CANDIDATES,
) -> list[dict]:
    """
    Resolve "<street> <number>[, <municipality>]" to ONE point per candidate street.

    Before (revision items 6/14/22/23) the whole text ("Marcel Thiry 52") was matched as a street name and the
    number was never used, so the chosen point was arbitrary. Now:
      1. candidate streets = (street_id, municipality) pairs whose fuzzy street name matches; a stated municipality
         adds a bonus, so "Rue X 29 - Jette" prefers the Jette street;
      2. on each candidate street the address with that EXACT house number is taken (policenum compared
         case/space-insensitively; the several rows of one number -- box/apartment numbers -- are collapsed to one
         point);
      3. if the number does not exist on that street: the nearest number with the SAME PARITY (same side of the street),
         flagged number_exact = False.
    Each candidate carries street_gid (the street group it belongs to) so the router can run along that street.
    """
    needle = _fold_needle(street_text)
    num = (house_number or "").replace(" ", "").upper() or None
    num_int = int(re.sub(r"\D", "", num)) if num and re.search(r"\d", num) else None
    muni = _expand_abbreviations(municipality_text) if municipality_text else None
    sql = """
        SET pg_trgm.word_similarity_threshold = 0.4;
        WITH streets AS (
            SELECT street_id, municipality_id,
                   MIN(street_name_fr) AS street_name_fr, MIN(street_name_nl) AS street_name_nl,
                   MIN(municipality_name_fr) AS municipality_name_fr, MIN(municipality_name_nl) AS municipality_name_nl,
                   MAX(GREATEST(
                       similarity(street_name_fr_norm, %(needle)s), similarity(street_name_nl_norm, %(needle)s),
                       word_similarity(%(needle)s, street_name_fr_norm), word_similarity(%(needle)s, street_name_nl_norm))) AS sim
            FROM urbis.mv_addresses_lookup
            WHERE (street_name_fr_norm %% %(needle)s OR street_name_nl_norm %% %(needle)s
                   OR %(needle)s <%% street_name_fr_norm OR %(needle)s <%% street_name_nl_norm)
            GROUP BY street_id, municipality_id
        ), scored AS (
            SELECT s.*,
                   s.sim
                   + CASE WHEN btrim(regexp_replace(unaccent(lower(COALESCE(s.street_name_fr, ''))), '[^a-z0-9]+', ' ', 'g')) = %(needle)s
                           OR btrim(regexp_replace(unaccent(lower(COALESCE(s.street_name_nl, ''))), '[^a-z0-9]+', ' ', 'g')) = %(needle)s
                          THEN %(exact_bonus)s ELSE 0 END
                   + CASE WHEN %(muni)s::text IS NOT NULL AND (
                            similarity(btrim(regexp_replace(unaccent(lower(COALESCE(s.municipality_name_fr, ''))), '[^a-z0-9]+', ' ', 'g')), %(muni)s) > 0.5
                         OR similarity(btrim(regexp_replace(unaccent(lower(COALESCE(s.municipality_name_nl, ''))), '[^a-z0-9]+', ' ', 'g')), %(muni)s) > 0.5)
                          THEN 0.3 ELSE 0 END AS score
            FROM streets s
            ORDER BY score DESC LIMIT %(fetch)s
        )
        SELECT sc.street_id, sc.street_name_fr, sc.street_name_nl, sc.municipality_name_fr, sc.municipality_name_nl, sc.score,
               a.matched, a.exact, a.rows_for_number, ST_AsText(a.pt) AS wkt, sg.gid AS street_gid
        FROM scored sc
        LEFT JOIN LATERAL (
            SELECT m.policenum AS matched, (m.norm = %(num)s) AS exact, count(*) AS rows_for_number,
                   ST_Centroid(ST_Collect(m.geometry)) AS pt
            FROM (SELECT policenum, upper(regexp_replace(policenum, '\\s', '', 'g')) AS norm,
                         NULLIF(regexp_replace(policenum, '\\D', '', 'g'), '')::int AS n, geometry
                  FROM urbis.mv_addresses_lookup x
                  WHERE x.street_id = sc.street_id AND x.municipality_id IS NOT DISTINCT FROM sc.municipality_id) m
            GROUP BY m.policenum, m.norm, m.n
            ORDER BY (m.norm = %(num)s) DESC NULLS LAST,
                     (m.n %% 2 <> %(parity)s) NULLS LAST,
                     abs(m.n - %(num_int)s) NULLS LAST
            LIMIT 1
        ) a ON %(num)s::text IS NOT NULL
        LEFT JOIN LATERAL (
            SELECT g.gid FROM urbis.street_groups g WHERE sc.street_id = ANY(g.pids)
            ORDER BY ST_Distance(g.geom, a.pt) LIMIT 1
        ) sg ON true
        ORDER BY sc.score DESC
    """
    rows = query(sql, {
        "needle": needle, "num": num, "num_int": num_int, "parity": (num_int % 2) if num_int is not None else None,
        "muni": muni, "exact_bonus": EXACT_NAME_BONUS, "fetch": limit * 3,
    })
    out = []
    for r in rows:
        if num and not r.get("wkt"):
            continue
        label_num = r.get("matched") or num or ""
        out.append({
            "id": f"ADDRESS:{r['street_id']}:{label_num}",
            "type": "ADDRESS",
            "name_fr": f"{r['street_name_fr']} {label_num}, {r['municipality_name_fr']}".strip(", "),
            "name_nl": f"{r['street_name_nl']} {label_num}, {r['municipality_name_nl']}".strip(", "),
            "wkt": r.get("wkt"), "centroid_wkt": r.get("wkt"),
            "score": float(r["score"]),
            "number_exact": bool(r.get("exact")), "matched_number": r.get("matched"), "requested_number": num,
            "street_gid": r.get("street_gid"),
        })
    return out[:limit]
