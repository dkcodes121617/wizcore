"""`core.external_actions` — the idempotency ledger.

Anything irreversible and outward-facing claims a key here **before** the API
call and records the outcome **after**. One timeout plus one retry otherwise
equals a duplicate public post that cannot be un-posted, or a second cold email
to someone who already got one.

The four states, and why each behaves the way it does:

| Ledger row | Meaning | What happens |
|---|---|---|
| absent | never attempted | claim it, proceed |
| `completed_at` NULL | claimed, outcome unknown | **refuse** |
| `ok = true` | done | skip, return the recorded result |
| `ok = false` | definitively failed | allow a fresh attempt |

The third row of that table is the one worth arguing about. A claim with no
completion means either a run is holding it right now, or a run died between
the claim and the response — and in the second case *we genuinely do not know
whether the post went out*. Refusing may cost one skipped post. Retrying may
cost a duplicate on a public account. For an action that cannot be undone, the
skip is obviously the cheaper mistake, so this refuses and reports rather than
guessing. Clearing a stuck claim is a deliberate human act.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from wizcore.db.conn import connect, fetch_one

log = logging.getLogger("wizcore.db.actions")


class ActionBlocked(RuntimeError):
    """The action was not claimable. Skip it; do not retry in this run."""


@dataclass
class Claim:
    key: str
    granted: bool
    reason: str = ""
    prior_result: dict = field(default_factory=dict)
    dry_run: bool = False
    _result: dict = field(default_factory=dict)
    _ok: bool | None = None

    def succeeded(self, **result) -> None:
        self._ok = True
        self._result = result

    def failed(self, **result) -> None:
        self._ok = False
        self._result = result


def make_key(*parts: str) -> str:
    """A deterministic idempotency key.

    The readable prefix is kept so a human reading the table can tell what a row
    is without decoding a hash, and the digest keeps the key bounded and unique
    over the full content.

        make_key("content_poster", "publish_facebook", "2026-08-06", body)
        -> 'content_poster:publish_facebook:2026-08-06:9f2c1a7b4e6d8035'
    """
    clean = [str(p).strip() for p in parts if str(p).strip()]
    readable = ":".join(clean[:3])
    digest = hashlib.sha256("|".join(clean).encode("utf-8")).hexdigest()[:16]
    return f"{readable}:{digest}"


@contextmanager
def claim(
    key: str,
    *,
    agent: str,
    kind: str,
    target: str = "",
    dry_run: bool = False,
    url: str | None = None,
) -> Iterator[Claim]:
    """Claim an irreversible action, then record its outcome.

        with claim(k, agent="content_poster", kind="publish_facebook",
                   target=page_id, dry_run=CONFIG.dry_run) as c:
            if not c.granted:
                log.info("skipping: %s", c.reason)
            else:
                res = graph_api_publish(...)
                c.succeeded(post_id=res["id"])

    In `dry_run` the ledger is not touched at all, but the claim is granted and
    the block runs — so the dry path exercises exactly the same code as the live
    path, which is the only way a dry run proves anything.
    """
    if dry_run:
        c = Claim(key=key, granted=True, reason="dry_run", dry_run=True)
        yield c
        log.info("dry_run: would record %s ok=%s", key, c._ok)
        return

    with connect(url, autocommit=True) as conn:
        row = fetch_one(
            conn,
            "INSERT INTO core.external_actions (idempotency_key, agent, kind, target) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING "
            "RETURNING idempotency_key",
            (key, agent, kind, target or None),
        )
        if row is None:
            existing = fetch_one(
                conn,
                "SELECT completed_at, ok, result FROM core.external_actions "
                "WHERE idempotency_key = %s",
                (key,),
            ) or {}
            if existing.get("completed_at") is None:
                yield Claim(
                    key=key,
                    granted=False,
                    reason="claimed by another run and never completed - outcome unknown, "
                           "refusing to risk a duplicate",
                )
                return
            if existing.get("ok"):
                yield Claim(
                    key=key,
                    granted=False,
                    reason="already completed successfully",
                    prior_result=existing.get("result") or {},
                )
                return
            # Known failure: safe to try again. Reopen the same row rather than
            # inserting a second one, so the ledger keeps one row per action.
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE core.external_actions SET requested_at = now(), "
                    "completed_at = NULL, ok = NULL WHERE idempotency_key = %s",
                    (key,),
                )

        c = Claim(key=key, granted=True)
        try:
            yield c
        except Exception as exc:
            c.failed(error=repr(exc)[:500])
            _record(conn, c)
            raise
        # A block that returns without calling succeeded()/failed() has not told
        # us what happened. Treat that as unknown and leave the row open: the
        # next run will refuse, which is the correct response to "we don't know".
        if c._ok is None:
            log.warning("action %s left no outcome; row stays open", key)
            return
        _record(conn, c)


def _record(conn, c: Claim) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE core.external_actions SET completed_at = now(), ok = %s, "
            "result = %s::jsonb WHERE idempotency_key = %s",
            (c._ok, json.dumps(c._result, default=str), c.key),
        )


def open_claims(conn, agent: str, older_than_minutes: int = 60) -> list[dict]:
    """Claims with no outcome — the rows that will block future attempts.

    Surfaced in the daily digest, because a stuck claim silently stops one
    action forever and nothing else in the system will mention it.
    """
    from wizcore.db.conn import fetch_all

    return fetch_all(
        conn,
        "SELECT idempotency_key, kind, target, requested_at FROM core.external_actions "
        "WHERE agent = %s AND completed_at IS NULL "
        "AND requested_at < now() - make_interval(mins => %s) ORDER BY requested_at",
        (agent, older_than_minutes),
    )
