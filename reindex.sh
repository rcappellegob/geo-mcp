#!/usr/bin/env bash
# reindex.sh -- maintenance of the geo-mcp database. Run it on the HOST (it calls `podman exec pg17 psql`).
#
#   ./reindex.sh status            embeddings done/missing, vector index size, current ivfflat.probes (changes nothing)
#   ./reindex.sh vector            rebuild the vector index + set ivfflat.probes   (run after the embeddings backfill)
#   ./reindex.sh collation --yes   REINDEX DATABASE + ALTER DATABASE ... REFRESH COLLATION VERSION
#                                  (removes the "collation version mismatch" warning for good)
#
# Why `vector`: the ivfflat index was created on an EMPTY table, so its list centres are meaningless;
# rebuilding it on the filled table is what makes the embedding search usable. ivfflat.probes defaults
# to 1 (1 of the 100 lists is scanned); 10 trades a little speed for much better recall. Tune with
# PROBES=20 ./reindex.sh vector .
#
# Why `collation`: the database was created with glibc 2.36 and now runs on 2.31, so text indexes may be
# ordered differently from what the server expects. Take a backup first, e.g.
#   podman exec pg17 pg_dump -U chatadmin -Fc mcp_db > /srv/resources/urbis/mcp_db_$(date +%F).dump
# It uses REINDEX ... CONCURRENTLY: no downtime, but it needs free disk space (a second copy of the
# index being rebuilt) and takes longer than a plain REINDEX.
#
# Overrides (environment): PSQL (full psql command), DB (database name), PROBES, INDEX.
set -euo pipefail

DB="${DB:-mcp_db}"
PROBES="${PROBES:-10}"
INDEX="${INDEX:-urbis.idx_gazetteer_embeddings_ivfflat}"
PSQL="${PSQL:-podman exec -i pg17 psql -U chatadmin -d ${DB} -X -v ON_ERROR_STOP=1}"

mode="${1:-}"
confirm="${2:-}"

# The server prints the collation warning on EVERY connection; hide it (it cannot be switched off with
# client_min_messages: it is printed before that setting applies). The real fix is the `collation` mode.
hide() { grep -vE '^(WARNING:  database ".*" has a collation version mismatch|DETAIL:  The database was created using collation version|HINT:  Rebuild all objects in this database)' || true; }
run() { $PSQL "$@" 2> >(hide >&2); }

status() {
  echo "== embeddings per type (missing = no embedding yet)"
  run -c "SELECT g.type, count(*) FILTER (WHERE e.gazetteer_id IS NULL) AS missing, count(*) AS total
          FROM urbis.gazetteer g LEFT JOIN urbis.gazetteer_embeddings e ON e.gazetteer_id = g.id
          WHERE COALESCE(g.name_fr, g.name_nl) IS NOT NULL GROUP BY 1 ORDER BY 1"
  echo "== vector index"
  run -c "SELECT c.relname AS index, pg_size_pretty(pg_relation_size(c.oid)) AS size, i.indisvalid AS valid
          FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
          WHERE c.relname LIKE 'idx_gazetteer_embeddings_ivfflat%'"
  echo "== ivfflat.probes in a new session (NULL = never set, so the default 1 applies)"
  run -qAtc "SELECT current_setting('ivfflat.probes', true)"
}

vector() {
  local missing
  missing=$(run -qAtc "SELECT count(*) FROM urbis.gazetteer g LEFT JOIN urbis.gazetteer_embeddings e ON e.gazetteer_id = g.id
                       WHERE e.gazetteer_id IS NULL AND COALESCE(g.name_fr, g.name_nl) IS NOT NULL")
  if [ "${missing}" != "0" ]; then
    echo "NOTE: ${missing} gazetteer rows still have no embedding. Rebuilding now is fine, but run this again"
    echo "      after the backfill is complete, because the index is built from the rows that exist now."
  fi
  echo "== rebuilding ${INDEX} (CONCURRENTLY: reads keep working)"
  # separate -c options = separate transactions in ONE session, which CONCURRENTLY requires
  run -c "SET maintenance_work_mem = '512MB'" \
      -c "REINDEX INDEX CONCURRENTLY ${INDEX}" \
      -c "ANALYZE urbis.gazetteer_embeddings"
  echo "== ivfflat.probes = ${PROBES} for database ${DB} (applies to new connections)"
  run -c "ALTER DATABASE ${DB} SET ivfflat.probes = ${PROBES}"
  status
}

collation() {
  if [ "${confirm}" != "--yes" ]; then
    echo "This reindexes every index of ${DB} (CONCURRENTLY) and marks the collation version as current."
    echo "Make a backup first (see the header of this script), then run:  $0 collation --yes"
    exit 1
  fi
  echo "== REINDEX DATABASE ${DB} CONCURRENTLY (can take a long while on the spatial indexes)"
  run -c "REINDEX DATABASE CONCURRENTLY ${DB}"
  echo "== ALTER DATABASE ${DB} REFRESH COLLATION VERSION"
  run -c "ALTER DATABASE ${DB} REFRESH COLLATION VERSION"
  echo "== check: a fresh connection must print NO warning (empty line below = fixed)"
  warn=$($PSQL -qAtc "SELECT 1" 2>&1 >/dev/null | head -3 || true)
  echo "${warn}"
  [ -z "${warn}" ] && echo "OK: warning gone." || echo "Warning still present: see above."
}

case "${mode}" in
  status)    status ;;
  vector)    vector ;;
  collation) collation ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
