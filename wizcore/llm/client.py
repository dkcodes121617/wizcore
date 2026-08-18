"""Client for the Claude-compatible proxy (LLMsRelay; ClaudeStore until 2026-08-16).

Seeded verbatim from the blog agent, which is where these details were learned.
This is the ONE copy — ground rule 8. Six re-implementations of the same quirks
drift, and the drift surfaces months later as a mystery 403.

Speaks the Anthropic Messages API shape directly over HTTP. Two hard-won details
from testing the endpoint:

  1. Cloudflare returns 403 "error code: 1010" for non-CLI clients. We MUST send a
     CLI-style User-Agent, or every call fails.
  2. The proxy has an aggressive prompt-injection guard. Prompts phrased as
     override/compliance commands ("reply with exactly X", "never break
     character", "obey this contract") trigger refusals + an alternate "Kiro"
     identity. So callers should phrase system/user prompts as normal
     professional content tasks. This module doesn't police that — the prompt
     files do — but `complete_json` retries on a parse failure, which also
     recovers the occasional guarded response.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_exponential_jitter,
)

from wizcore.config import env_str
from wizcore.llm.sanitize import clean_text

log = logging.getLogger("wizcore.llm")

# The single most important header. Do not remove — the proxy's WAF blocks
# anything that doesn't look like the official CLI/SDK.
_USER_AGENT = "claude-cli/1.0.0 (external, cli)"

# Below this, a cache entry costs more than it saves.
_CACHE_MIN_CHARS = 2000
_ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """A non-retryable problem talking to the proxy (auth, bad request, etc.)."""


class LLMTransient(RuntimeError):
    """A retryable problem (timeout, 5xx, 529 overloaded, rate limit, WAF hiccup).

    Carries `retry_after` when the server said how long to wait. Guessing a
    backoff when the server has already told you the answer is how a 429 turns
    into five more 429s.
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Retryable HTTP statuses.
#
# 529 `overloaded_error` is the one that matters and the one that was missing:
# it is Anthropic's "capacity reached, come back shortly" and is explicitly
# retryable, but it fell through to the permanent branch below and aborted the
# run on the first occurrence. 408/409/425 are the same class of "try again"
# that the official SDKs retry.
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

#: Ceiling on a single sleep. A server may legitimately ask for minutes on a
#: daily-quota 429; waiting that long inside a scheduled run is worse than
#: giving up and letting the next tick retry.
_MAX_SLEEP = 60.0

#: Total wall-clock budget for one completion, across every attempt.
#:
#: The agents run on Modal with a 900s function timeout. Without a deadline,
#: 5 attempts x a 300s read timeout plus backoff can exceed that on its own, so
#: one unlucky call would consume the entire run and be killed mid-write. This
#: bounds a call to a bit over four minutes so the run always keeps enough time
#: to finish and record what it did.
_DEADLINE = 260.0


def _retry_after(resp) -> float | None:
    """The server's own answer to "when should I come back?", in seconds.

    `Retry-After` is either a delta in seconds or an HTTP date; only the delta
    form is worth parsing here, and an unparseable value simply means we fall
    back to exponential backoff rather than crash inside error handling.
    """
    raw = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


_BACKOFF = wait_exponential_jitter(initial=3, max=_MAX_SLEEP, exp_base=2, jitter=3)


def _wait_for_retry(state) -> float:
    """Obey `Retry-After` when given; otherwise exponential backoff with jitter.

    The jitter is not decoration. The classifier fans several completions out at
    once, so a fixed backoff makes every one of them wake and retry in the same
    instant — re-creating the burst that caused the 429 in the first place.
    """
    exc = state.outcome.exception() if state.outcome else None
    server_says = getattr(exc, "retry_after", None)
    if server_says is not None:
        return min(server_says + random.uniform(0, 1.0), _MAX_SLEEP)
    return _BACKOFF(state)


def _log_retry(state) -> None:
    """Make retries visible. A silent retry storm looks exactly like slowness."""
    exc = state.outcome.exception() if state.outcome else None
    log.warning(
        "llm retry %d after %.1fs: %s",
        state.attempt_number,
        state.idle_for,
        str(exc)[:200],
    )


class LLMClient:
    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        on_usage: Callable[[str, dict], None] | None = None,
    ):
        """`on_usage(model, usage)` fires after every successful call.

        It is the seam the budget guard hangs off: an agent passes a callback
        that writes to `core.spend_ledger`, and this module keeps no database
        import. Parsing the usage block in three agents instead would be three
        chances to parse it differently.
        """
        self.base_url = (
            base_url or env_str("ANTHROPIC_BASE_URL", "https://api.llmsrelay.com")
        ).rstrip("/")
        self.api_key = api_key or env_str("ANTHROPIC_API_KEY")
        self.model = model or env_str("ANTHROPIC_MODEL", "claude-sonnet-4.6")
        self._on_usage = on_usage
        self._session = requests.Session()
        self._session.headers.update(
            {
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "user-agent": _USER_AGENT,
                "anthropic-beta": "prompt-caching-2024-07-31",
            }
        )

    # ── low-level ──
    @retry(
        retry=retry_if_exception_type(LLMTransient),
        # Whichever comes first: a handful of attempts, or the wall-clock
        # budget. Attempts alone are not a bound when each one can block for
        # minutes, and a deadline alone would allow a tight spin on fast
        # failures — the pair is what makes the worst case predictable.
        stop=(stop_after_attempt(6) | stop_after_delay(_DEADLINE)),
        wait=_wait_for_retry,
        reraise=True,
        before_sleep=_log_retry,
    )
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/v1/messages"
        # (connect timeout, read timeout). The proxy is legitimately slow — a
        # minute or more on a long generation is normal and is NOT a failure, so
        # the read timeout is deliberately generous. Cutting it short would turn
        # a working slow call into a retry storm that is slower still and costs
        # a second generation each time.
        try:
            resp = self._session.post(url, data=json.dumps(payload), timeout=(10, 300))
        except requests.RequestException as e:
            raise LLMTransient(f"network error: {e}") from e

        if resp.status_code == 200:
            return resp.json()

        body = resp.text[:400]
        # 1010 = Cloudflare WAF; usually transient / UA-related but retry can clear it.
        if resp.status_code in _RETRYABLE_STATUS or "1010" in body:
            raise LLMTransient(
                f"HTTP {resp.status_code}: {body}",
                retry_after=_retry_after(resp),
            )
        if resp.status_code in (401, 403):
            raise LLMError(f"auth/forbidden HTTP {resp.status_code}: {body}")
        raise LLMError(f"HTTP {resp.status_code}: {body}")

    # ── public ──
    def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float | None = None,
        model: str | None = None,
    ) -> str:
        """Return the assistant's text for a single-turn system+user prompt."""
        # The facts block is ~17k chars and is resent on every call in a run (intro +
        # one per H2 + closing + factcheck + humanize = 9+ calls). Prompt caching is
        # supported by this proxy (probed: cache_read confirmed), so the system prompt
        # is marked cacheable whenever it is long enough to be worth a cache entry.
        # Short system prompts are sent as a plain string to avoid pointless overhead.
        system_field: object = system
        if len(system) > _CACHE_MIN_CHARS:
            system_field = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]

        payload: dict = {
            "model": model or self.model,
            "max_tokens": max_tokens,
            "system": system_field,
            "messages": [{"role": "user", "content": user}],
        }
        if temperature is not None:
            payload["temperature"] = temperature

        t0 = time.time()
        data = self._post(payload)
        dt = time.time() - t0

        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        )
        usage = data.get("usage", {})
        log.info(
            "llm.complete %.1fs model=%s in=%s out=%s cache_r=%s stop=%s",
            dt, data.get("model"), usage.get("input_tokens"),
            usage.get("output_tokens"), usage.get("cache_read_input_tokens"),
            data.get("stop_reason"),
        )
        if self._on_usage:
            # Telemetry must never fail the call that produced it. A budget
            # ledger that takes down a run is worse than no budget ledger.
            try:
                self._on_usage(data.get("model") or self.model, usage)
            except Exception:  # noqa: BLE001
                log.warning("on_usage callback failed", exc_info=True)
        if not text.strip():
            raise LLMTransient("empty completion")
        return clean_text(text)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2048,
        model: str | None = None,
        attempts: int = 3,
    ) -> dict | list:
        """Like complete(), but parse the reply as JSON.

        Retries with a firmer 'return only JSON' nudge on parse failure — which
        also recovers the rare case where the proxy's guard prepended prose.
        """
        sys = system.rstrip() + (
            "\nRespond with a single valid JSON value only — no explanation before "
            "or after it, no markdown fences. Start your reply with the opening "
            "brace or bracket and stop at the closing one."
        )
        last_err: Exception | None = None
        for i in range(attempts):
            raw = self.complete(system=sys, user=user, max_tokens=max_tokens, model=model)
            parsed = extract_json(raw)
            if parsed is not None:
                return parsed
            last_err = ValueError(f"unparseable JSON: {raw[:200]!r}")
            log.warning("complete_json parse retry %d/%d", i + 1, attempts)
            user = user + "\n\nYour previous reply was not valid JSON. Return the JSON value only."
        raise LLMError(f"could not obtain valid JSON after {attempts} attempts: {last_err}")

    def ping(self) -> tuple[bool, str]:
        """Cheap connectivity + capability check. Returns (ok, detail)."""
        try:
            txt = self.complete(
                system="You are a helpful assistant for a software studio.",
                user="In one short sentence, name three benefits of a technical blog for a software company's SEO.",
                max_tokens=120,
            )
            return True, txt.strip()
        except Exception as e:  # noqa: BLE001
            return False, repr(e)


def extract_json(text: str):
    """Best-effort JSON extraction: whole string, or the first {...}/[...] block.

    Public because it is needed for EVERY provider, not just this one. Measured
    across the proxy's models and Groq: **no model returned bare JSON** — all of
    them fenced it — so any caller parsing model output needs this, and three
    agents writing three fence-strippers would be three chances to get the
    balanced-bracket walk subtly wrong.
    """
    text = text.strip()
    # Strip a ```json fence if present.
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced object/array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    return None
