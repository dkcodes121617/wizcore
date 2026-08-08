"""Read and rotate machine-refreshed tokens.

## Why this exists

Instagram, Threads and Pinterest tokens all expire and all three are
refreshable indefinitely — so no human should ever have to re-authorise them.
Before this, one did, for a single mechanical reason: **a Modal container cannot
write its own secret.** The weekly cron refreshed the token correctly and then
exited, taking the new value with it. Next run, same stale secret.

Moving rotating tokens into `core.agent_credentials` breaks that loop. A cron
refreshes and persists; every agent reads the database first and falls back to
the environment.

## Read order, and why it is this way round

    database  ->  environment  ->  ""

Database first, because it is the *fresher* of the two by construction: the env
holds whatever was true at deploy time, the table holds whatever was true at the
last rotation. Falling back to the environment means the system still works
before the first rotation, on a fresh database, and in local development where
the table may not exist at all.

Cached per process. These are read on nearly every API call and a container
lives for seconds; re-querying Postgres each time would add a round trip to
every publish for a value that cannot change mid-run.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from wizcore.config import env_str
from wizcore.db.conn import connect, fetch_all

log = logging.getLogger("wizcore.db.credentials")

_cache: dict[str, str] = {}
_loaded = False


def load(url: str | None = None) -> dict[str, str]:
    """Load every stored credential once per process. Never raises.

    A failure here is not fatal and must not be: the environment fallback is a
    complete answer on its own, and an unreachable credentials table should
    degrade to "use what was deployed" rather than take the run down.
    """
    global _loaded
    if _loaded:
        return _cache
    try:
        with connect(url, autocommit=True) as conn:
            rows = fetch_all(conn, "SELECT name, value FROM core.agent_credentials")
        _cache.update({r["name"]: r["value"] for r in rows if r["value"]})
        log.info("loaded %d rotated credential(s)", len(_cache))
    except Exception:  # noqa: BLE001
        log.warning("credential store unavailable; using environment only", exc_info=True)
    _loaded = True
    return _cache


def get(name: str, url: str | None = None) -> str:
    """The freshest known value: database, then environment, then ''."""
    return load(url).get(name) or env_str(name)


def put(
    name: str,
    value: str,
    owner_agent: str,
    expires_at: datetime | None = None,
    url: str | None = None,
    notes: str = "",
) -> bool:
    """Persist a rotated credential. Returns True on success.

    Refuses to store an empty value. Pinterest omits the refresh token on some
    responses when the existing one is still valid, and writing that absence
    back would blank a working credential — turning a routine refresh into a
    dead integration, which is exactly the failure this module exists to end.
    """
    if not value:
        log.warning("refusing to store empty value for %s", name)
        return False
    try:
        with connect(url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO core.agent_credentials
                    (name, value, owner_agent, expires_at, rotated_at, rotations, notes)
                VALUES (%s, %s, %s, %s, now(), 1, %s)
                ON CONFLICT (name) DO UPDATE SET
                    value      = EXCLUDED.value,
                    expires_at = EXCLUDED.expires_at,
                    rotated_at = now(),
                    rotations  = core.agent_credentials.rotations + 1,
                    last_error = NULL,
                    notes      = COALESCE(EXCLUDED.notes, core.agent_credentials.notes)
                """,
                (name, value, owner_agent, expires_at, notes or None),
            )
        _cache[name] = value
        log.info("rotated credential %s", name)
        return True
    except Exception:  # noqa: BLE001
        log.error("could not persist rotated credential %s", name, exc_info=True)
        return False


def record_failure(name: str, error: str, owner_agent: str, url: str | None = None) -> None:
    """Note a failed rotation without touching the value.

    The old token is usually still valid for weeks after a refresh fails, so the
    right response to a failure is to keep using it and raise an alarm — not to
    discard it.
    """
    try:
        with connect(url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO core.agent_credentials (name, value, owner_agent, last_error) "
                "VALUES (%s, '', %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET last_error = EXCLUDED.last_error",
                (name, owner_agent, error[:400]),
            )
    except Exception:  # noqa: BLE001
        log.debug("could not record rotation failure for %s", name, exc_info=True)


def expiring(days: int = 14, url: str | None = None) -> list[dict]:
    """Credentials expiring inside `days`, or whose last rotation failed.

    This is what makes a stalled refresher visible. A token that quietly stopped
    rotating looks identical to one that never needed to — until it expires on a
    weekend.
    """
    try:
        with connect(url, autocommit=True) as conn:
            return fetch_all(
                conn,
                "SELECT name, owner_agent, expires_at, rotated_at, rotations, last_error "
                "FROM core.agent_credentials "
                "WHERE (expires_at IS NOT NULL AND expires_at < now() + make_interval(days => %s)) "
                "   OR last_error IS NOT NULL "
                "ORDER BY expires_at NULLS LAST",
                (days,),
            )
    except Exception:  # noqa: BLE001
        return []


def stale_rotations(max_age_days: int = 45, url: str | None = None) -> list[dict]:
    """Credentials that should have rotated by now and have not.

    The complement of `expiring`: this catches a refresher that is failing
    *silently* — no error recorded, no exception, simply never running. That is
    the failure mode a weekly cron is most likely to have, and the one nothing
    else would notice until the token died.
    """
    try:
        with connect(url, autocommit=True) as conn:
            return fetch_all(
                conn,
                "SELECT name, owner_agent, rotated_at, rotations FROM core.agent_credentials "
                "WHERE rotated_at < now() - make_interval(days => %s) "
                "ORDER BY rotated_at",
                (max_age_days,),
            )
    except Exception:  # noqa: BLE001
        return []


def seconds_to_expiry(name: str, url: str | None = None) -> int | None:
    for row in expiring(3650, url):
        if row["name"] == name and row["expires_at"]:
            return int((row["expires_at"] - datetime.now(UTC)).total_seconds())
    return None
