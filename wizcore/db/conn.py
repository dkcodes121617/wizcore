"""Postgres access. One database, one contract, one connection style.

Neon's free plan is **100 CU-hours per project per month** with autosuspend after
5 minutes of inactivity that cannot be shortened. The cost that matters is not
query time — it is that every wake-up holds compute alive for a 5-minute idle
window it cannot skip.

So the rule for every agent in this system is: **batch database work into one
short connection**, ideally at the end of a run, rather than opening one per
source or per node. Seven sources each opening their own connection does not
cost seven connections; it risks costing seven idle windows.

There is no pool here on purpose. A pool is for a long-lived server holding
connections warm. These are cron-triggered containers that run for seconds and
exit — a pool would add a shutdown path to get wrong for no benefit.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from wizcore.config import env_str

log = logging.getLogger("wizcore.db")


class DBError(RuntimeError):
    pass


def database_url() -> str:
    url = env_str("NEON_DATABASE_URL")
    if not url:
        raise DBError("NEON_DATABASE_URL is not set")
    return url


@contextmanager
def connect(url: str | None = None, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """A connection that commits on clean exit and rolls back on exception.

    Rows come back as dicts. Tuples are compact but positional, and every
    consumer of a positional row breaks silently the day a column is added to
    the middle of a SELECT.
    """
    conn = psycopg.connect(url or database_url(), row_factory=dict_row, connect_timeout=15)
    conn.autocommit = autocommit
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(conn: psycopg.Connection, sql: str, params: Any = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(conn: psycopg.Connection, sql: str, params: Any = None) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(conn: psycopg.Connection, sql: str, params: Any = None) -> int:
    """Run a statement, return the affected row count."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def ping(url: str | None = None) -> tuple[bool, str]:
    """Cheap liveness check for a preflight node."""
    try:
        with connect(url, autocommit=True) as conn:
            row = fetch_one(conn, "select version() as v")
            return True, (row or {}).get("v", "")[:60]
    except Exception as e:  # noqa: BLE001
        return False, repr(e)
