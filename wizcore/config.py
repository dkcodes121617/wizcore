"""Environment loading and typed configuration access.

Precedence, low to high:

  1. `<agent folder>/.env`
  2. the real process environment — **wins**

That order is what lets Modal and GitHub Actions inject values over the file
without the file having to know it is running in production.

Every agent calls `load_env(AGENT_ROOT)` once at import time and then reads
through the typed getters here. Nothing in an agent reads `os.environ` directly:
the `_clean()` pass below is not optional, and a raw `os.getenv` silently skips
it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required configuration value is missing or unusable."""


# python-dotenv keeps a trailing inline `# comment` as part of an unquoted
# value, which has bitten this codebase before — a token with a stray comment
# after it authenticates as garbage and the error says nothing useful.
#
# This strips the comment ONLY when the '#' is preceded by whitespace, which is
# what a dotenv trailing comment always looks like. The blog agent's original
# split on the first '#' anywhere, which would quietly truncate a password
# containing one. Same fix, one less footgun.
_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _clean(raw: str | None) -> str:
    """Strip whitespace and any trailing inline '# comment'."""
    if raw is None:
        return ""
    return _TRAILING_COMMENT.sub("", raw).strip()


def load_env(agent_root: str | Path) -> None:
    """Load `<agent_root>/.env` without overriding the real environment.

    `encoding='utf-8-sig'` is deliberate: these files have been written with a
    BOM before, and a BOM turns the first key into `\\ufeffANTHROPIC_BASE_URL`,
    which then reads as missing. Tolerate it rather than rediscover it.
    """
    env_path = Path(agent_root) / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False, encoding="utf-8-sig")


# ── typed getters ──
def env_str(name: str, default: str = "") -> str:
    return _clean(os.getenv(name)) or default


def env_int(name: str, default: int) -> int:
    try:
        return int(_clean(os.getenv(name)) or default)
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(_clean(os.getenv(name)) or default)
    except (TypeError, ValueError):
        return default


def env_bool(name: str, default: bool) -> bool:
    raw = _clean(os.getenv(name))
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def env_list(name: str, default: str = "") -> list[str]:
    """Comma-separated list — PLATFORMS_ENABLED, SOURCES_ENABLED, CHANNELS_ENABLED.

    Empty entries are dropped, so a trailing comma or a value edited down to
    nothing degrades to "none enabled" rather than to a list holding "".
    """
    raw = env_str(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def require(*names: str) -> None:
    """Fail fast, listing every missing key at once.

    Reporting them one per run turns a five-minute fix into five deploys, so
    this collects the whole set before raising.
    """
    missing = [n for n in names if not env_str(n)]
    if missing:
        raise ConfigError(
            "missing required configuration: " + ", ".join(missing)
            + "\nSet them with:  python tools/set_env.py KEY=VALUE"
        )
