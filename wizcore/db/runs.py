"""`core.agent_runs` — one row per run, for every agent.

Modal's log retention on the free tier is short and the two GitHub Actions
agents have no shared place to report into at all. This table is the durable
answer to "did everything run today, and what did it do?" — and it is what the
daily digest reads.

Usage is a context manager so the finished/failed bookkeeping cannot be skipped
on the error path, which is the path where it matters:

    with run_scope("lead_finder") as run:
        run.count(candidates=312, new_leads=4)
        ...
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from wizcore.db.conn import connect, execute
from wizcore.obs.log import traceback_tail

log = logging.getLogger("wizcore.db.runs")


class RunRecorder:
    """Accumulates counters in memory; writes once at the end.

    In memory rather than incrementally on purpose — see `wizcore.db.conn` for
    why this system does not open a connection per step.
    """

    def __init__(self, run_id: str, agent: str):
        self.run_id = run_id
        self.agent = agent
        self.counters: dict[str, int] = {}
        self.status = "running"
        self.error: str | None = None

    def count(self, **kw: int) -> None:
        for key, value in kw.items():
            self.counters[key] = self.counters.get(key, 0) + int(value)

    def set(self, **kw: int) -> None:
        for key, value in kw.items():
            self.counters[key] = int(value)

    def mark_partial(self, reason: str = "") -> None:
        """A run that lost a source or a platform but still delivered work.

        Distinct from 'failed' because it needs a different response: 'failed'
        means look now, 'partial' means look if it repeats.
        """
        self.status = "partial"
        if reason and not self.error:
            self.error = reason


@contextmanager
def run_scope(
    agent: str, run_id: str | None = None, url: str | None = None
) -> Iterator[RunRecorder]:
    """Open a run row, always close it.

    The database is written twice — once on entry so an in-flight run is visible
    while it runs, once on exit with the outcome. A crashed container that never
    reaches the exit write leaves a row stuck in 'running', which is exactly the
    signal the daily reconciliation query looks for.
    """
    rec = RunRecorder(run_id or str(uuid.uuid4()), agent)
    try:
        with connect(url, autocommit=True) as conn:
            execute(
                conn,
                "INSERT INTO core.agent_runs (run_id, agent, status) VALUES (%s, %s, 'running') "
                "ON CONFLICT (run_id) DO NOTHING",
                (rec.run_id, agent),
            )
    except Exception:  # noqa: BLE001
        # Never let bookkeeping stop the work. A run that did its job but could
        # not record that it did is strictly better than a run that refused to
        # start because the ledger was unreachable.
        log.warning("could not open agent_runs row", exc_info=True)

    try:
        yield rec
        if rec.status == "running":
            rec.status = "ok"
    except BaseException as exc:
        rec.status = "failed"
        rec.error = traceback_tail(exc)
        raise
    finally:
        try:
            with connect(url, autocommit=True) as conn:
                execute(
                    conn,
                    "UPDATE core.agent_runs SET finished_at = now(), status = %s, "
                    "counters = %s::jsonb, error = %s WHERE run_id = %s",
                    (rec.status, _json(rec.counters), rec.error, rec.run_id),
                )
        except Exception:  # noqa: BLE001
            log.warning("could not close agent_runs row", exc_info=True)


def _json(obj) -> str:
    import json

    return json.dumps(obj, default=str)


def recent_runs(conn, hours: int = 24) -> list[dict]:
    """Backs the daily digest."""
    from wizcore.db.conn import fetch_all

    return fetch_all(
        conn,
        "SELECT agent, status, started_at, finished_at, counters, error "
        "FROM core.agent_runs WHERE started_at > now() - make_interval(hours => %s) "
        "ORDER BY started_at DESC",
        (hours,),
    )


def stuck_runs(conn, older_than_minutes: int = 90) -> list[dict]:
    """Rows still 'running' long past any plausible runtime.

    A container killed mid-run leaves one of these behind. Nothing else in the
    system notices — that is the point of asking.
    """
    from wizcore.db.conn import fetch_all

    return fetch_all(
        conn,
        "SELECT run_id, agent, started_at FROM core.agent_runs "
        "WHERE status = 'running' AND started_at < now() - make_interval(mins => %s) "
        "ORDER BY started_at",
        (older_than_minutes,),
    )
