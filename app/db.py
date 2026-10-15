"""
app/db.py
=========
Small psycopg2 wrapper. All spatial resolution happens as SQL against the
urbis schema (gazetteer, mv_addresses_lookup, v_street_segments, ...), so we
keep this deliberately thin rather than pulling in an ORM.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg2
import psycopg2.extras

from app.config import db_dsn


def query(sql: str, params: Optional[list | tuple] = None) -> list[dict[str, Any]]:
    with psycopg2.connect(db_dsn()) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            if cur.description is None:
                return []
            return [dict(row) for row in cur.fetchall()]


def execute(sql: str, params: Optional[list | tuple] = None) -> None:
    with psycopg2.connect(db_dsn()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params or [])
