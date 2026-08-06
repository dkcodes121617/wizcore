"""Structured logging: JSON lines, every record carrying the run_id.

Modal's log retention on the free tier is short, so these lines are for reading
a run *while it is happening* or shortly after. The durable record is the
`core.agent_runs` row (see `wizcore.db.runs`) — the two are complementary and
neither replaces the other.

`run_id` travels in a ContextVar rather than as an argument to every log call.
Threading it through every function signature is the kind of change that gets
skipped under time pressure on exactly the one code path that later needs
tracing.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import traceback
from datetime import datetime, timezone

_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")
_agent: contextvars.ContextVar[str] = contextvars.ContextVar("agent", default="-")

# Never let a logging call raise into the agent. A run that dies inside its own
# telemetry is the worst possible failure: no work done, and the reason is the
# thing that was supposed to explain it.
logging.raiseExceptions = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "agent": _agent.get(),
            "run_id": _run_id.get(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Anything passed as extra={"extra_fields": {...}} joins the record.
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = "".join(traceback.format_exception(*record.exc_info))[-2000:]
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(agent: str, run_id: str, level: str = "INFO") -> None:
    """Install the JSON handler and bind agent + run_id for this context."""
    _agent.set(agent)
    _run_id.set(run_id)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Replace rather than append: Modal and some libraries install their own
    # basicConfig handler, and two handlers means every line printed twice.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)

    # These are chatty at DEBUG and say nothing we act on.
    for noisy in ("urllib3", "httpx", "httpcore", "botocore", "boto3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def bind(run_id: str | None = None, agent: str | None = None) -> None:
    """Rebind the context inside a worker thread or a resumed run."""
    if run_id is not None:
        _run_id.set(run_id)
    if agent is not None:
        _agent.set(agent)


def current_run_id() -> str:
    return _run_id.get()


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    """Log a line with structured fields attached.

        log_event(log, "source.done", source="hackernews", found=12)
    """
    logger.info(msg, extra={"extra_fields": fields})


def traceback_tail(exc: BaseException, limit: int = 1200) -> str:
    """The last `limit` characters of a formatted traceback.

    Telegram's message cap is 4096 characters and the useful part of a Python
    traceback is the end, so alerts carry the tail rather than a truncated head.
    """
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return text[-limit:]
