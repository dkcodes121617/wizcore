"""Retention — keep the database inside the free tier without losing history.

Neon's free plan caps storage at 500 MB. Nothing in this system deletes, so the
only question was when it would fill, not whether.

## What actually grows

Measured, not assumed. At 44 MB total:

    lf_ckpt (LangGraph checkpoints, Lead Finder)   29 MB   66%
    everything else combined                       15 MB

The checkpointer writes one row per node per run to `checkpoints`,
`checkpoint_blobs` and `checkpoint_writes`. The Lead Finder runs 48 times a day
and carries a few hundred candidates through the graph, so the blobs are large
and constant. That is roughly 3 MB a day, which reaches 500 MB in about five
months and would then stop every agent at once.

## Why checkpoints are safe to delete and leads are not

A checkpoint exists to resume an interrupted run. Once a run has a terminal
status in `core.agent_runs` it will never be resumed — the next tick starts a
new `thread_id`. So a checkpoint belonging to a finished run is dead weight the
moment the run ends, and this deliberately keeps a week of them anyway so a
recent failure can still be inspected.

The business data is not touched. Leads, entities, posts, outreach and the spend
ledger are the record of what the system did and are small: `core.leads` is
under a megabyte at 642 rows. Deleting those to save space would be trading the
thing you are paying to keep for the thing you are paying to throw away.

`content.trend_items` is the one exception worth pruning — 741 rows of raw
firehose at 1 MB, of which the rejected ones have already served their purpose
by the time anyone reads them.
"""
from __future__ import annotations

import logging

from wizcore.db.conn import connect, fetch_one

log = logging.getLogger("wizcore.db.retention")

#: A week. Long enough that a Friday failure is still inspectable on Monday.
CHECKPOINT_DAYS = 7

#: Rejected trend items are kept long enough to audit the classifier, no longer.
TREND_REJECTED_DAYS = 14

#: Runs are the audit trail and are tiny (104 kB at 66 rows), so this is
#: generous. It exists only so the table cannot grow without bound for years.
RUN_DAYS = 180

#: schema -> the agent whose run_ids are its thread_ids.
CHECKPOINT_SCHEMAS = {
    "lf_ckpt": "lead_finder",
    "cp_ckpt": "content_poster",
    "oa_ckpt": "outreach",
}


def prune(database_url: str, dry_run: bool = False) -> dict[str, int]:
    """Delete what is safe to delete. Returns row counts, never raises.

    Never raises because this runs inside a scheduled agent: a cleanup that
    takes down the run it is attached to has done more damage than the disk it
    saved. A failure is logged and the next tick tries again.
    """
    counts: dict[str, int] = {}
    try:
        with connect(database_url, autocommit=True) as conn:
            for schema, agent in CHECKPOINT_SCHEMAS.items():
                counts.update(_prune_checkpoints(conn, schema, agent, dry_run))
            counts.update(_prune_trends(conn, dry_run))
            counts.update(_prune_runs(conn, dry_run))
            counts["db_mb"] = _size_mb(conn)
    except Exception:  # noqa: BLE001
        log.warning("retention pass failed", exc_info=True)
    return counts


def _prune_checkpoints(conn, schema: str, agent: str, dry_run: bool) -> dict[str, int]:
    """Drop checkpoints for runs that reached a terminal status a week ago.

    Anchored to `core.agent_runs.finished_at` rather than to anything inside the
    checkpoint tables, because LangGraph does not store a plain timestamp and
    parsing one out of a checkpoint id would be guessing at its internals.

    A run still in 'running' is never touched at any age, so a long run can
    never have its own state deleted underneath it.
    """
    out: dict[str, int] = {}
    dead = (
        "SELECT run_id::text FROM core.agent_runs "
        "WHERE agent = %s AND status <> 'running' "
        f"  AND finished_at < now() - interval '{CHECKPOINT_DAYS} days'"
    )
    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        key = f"{schema}.{table}"
        try:
            if dry_run:
                row = fetch_one(
                    conn,
                    f"SELECT count(*) AS n FROM {schema}.{table} WHERE thread_id IN ({dead})",
                    (agent,),
                )
                out[key] = int((row or {}).get("n") or 0)
                continue
            with conn.cursor() as cur:
                cur.execute(
                    f"DELETE FROM {schema}.{table} WHERE thread_id IN ({dead})", (agent,)
                )
                out[key] = cur.rowcount or 0
        except Exception:  # noqa: BLE001
            # A schema that does not exist yet is normal: an agent that has
            # never run has no checkpointer tables.
            log.debug("could not prune %s", key, exc_info=True)
            out[key] = 0
    return out


def _prune_trends(conn, dry_run: bool) -> dict[str, int]:
    """Rejected and expired firehose items, once they can no longer be used.

    `verified`, `angled` and `scored` items are kept: those are the ones an
    angle cites, and `content.trend_sources` holds the extract a published claim
    was grounded in. Deleting the evidence for a live claim to save a megabyte
    would break the grounding gate's whole purpose.
    """
    sql = (
        "DELETE FROM content.trend_items WHERE status IN ('rejected','expired') "
        f"AND surfaced_at < now() - interval '{TREND_REJECTED_DAYS} days' "
        "AND id NOT IN (SELECT trend_id FROM content.trend_angles WHERE trend_id IS NOT NULL) "
        "AND id NOT IN (SELECT trend_id FROM content.trend_sources WHERE trend_id IS NOT NULL)"
    )
    return {"content.trend_items": _run(conn, sql, dry_run)}


def _prune_runs(conn, dry_run: bool) -> dict[str, int]:
    return {
        "core.agent_runs": _run(
            conn,
            "DELETE FROM core.agent_runs "
            f"WHERE finished_at < now() - interval '{RUN_DAYS} days'",
            dry_run,
        )
    }


def _run(conn, sql: str, dry_run: bool) -> int:
    try:
        if dry_run:
            counted = sql.replace("DELETE FROM", "SELECT count(*) AS n FROM", 1)
            return int((fetch_one(conn, counted) or {}).get("n") or 0)
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.rowcount or 0
    except Exception:  # noqa: BLE001
        log.debug("prune failed: %s", sql[:80], exc_info=True)
        return 0


def _size_mb(conn) -> int:
    row = fetch_one(conn, "SELECT pg_database_size(current_database()) AS b")
    return int((row or {}).get("b") or 0) // 1048576
