"""
app/geo/embeddings.py
======================
Backfills urbis.gazetteer_embeddings so the resolver's semantic fallback
(app/geo/resolver.py::_embedding_candidates) has something to search.
Run manually or on a schedule after urbis.refresh_gazetteer() runs -- e.g.
right after sync_urbis.py's weekly sync.

Usage:
    python -m app.geo.embeddings [--backend bge-m3]
"""

from __future__ import annotations

import argparse
import logging

from app.config import ACTIVE_EMBEDDING_BACKEND, EMBEDDING_DIMENSIONS
from app.db import execute, query
from app.llm import embed

log = logging.getLogger("embeddings")


def backfill(backend: str = ACTIVE_EMBEDDING_BACKEND, batch_size: int = 200) -> int:
    dim = EMBEDDING_DIMENSIONS.get(backend)
    if dim is None:
        raise ValueError(f"Unknown embedding backend {backend!r}")

    # Only (re)embed rows that are missing an embedding for this backend,
    # or whose gazetteer name changed since the last embedding (best-effort:
    # here we simply skip rows that already have a row for this model).
    rows = query(
        """
        SELECT g.id, COALESCE(g.name_fr, g.name_nl) AS name
        FROM urbis.gazetteer g
        LEFT JOIN urbis.gazetteer_embeddings e
               ON e.gazetteer_id = g.id AND e.model = %(model)s
        WHERE e.gazetteer_id IS NULL
          AND COALESCE(g.name_fr, g.name_nl) IS NOT NULL
        """,
        {"model": backend},
    )

    log.info("Backfilling %d gazetteer embeddings with backend %r (dim=%d)", len(rows), backend, dim)

    count = 0
    for row in rows:
        vec = embed(row["name"], backend=backend)
        if len(vec) != dim:
            raise ValueError(
                f"Backend {backend!r} returned a {len(vec)}-dim vector, "
                f"expected {dim} (check EMBEDDING_DIMENSIONS in app/config.py "
                f"and the vector(...) size in sql/lookup_views.sql)"
            )
        # psycopg2 has no native pgvector adapter: a plain Python list would
        # be sent as a Postgres ARRAY ('{1,2,3}'), which ::vector rejects.
        # Send the pgvector text literal format ('[1,2,3]') instead.
        vec_literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
        execute(
            """
            INSERT INTO urbis.gazetteer_embeddings (gazetteer_id, model, embedding, updated_at)
            VALUES (%s, %s, %s::vector, now())
            ON CONFLICT (gazetteer_id) DO UPDATE
                SET model = EXCLUDED.model,
                    embedding = EXCLUDED.embedding,
                    updated_at = now()
            """,
            [row["id"], backend, vec_literal],
        )
        count += 1
        if count % batch_size == 0:
            log.info("  ... %d/%d embedded", count, len(rows))

    log.info("Done: %d embeddings written", count)
    return count


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default=ACTIVE_EMBEDDING_BACKEND)
    args = parser.parse_args()
    backfill(backend=args.backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
