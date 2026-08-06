"""`core.spend_ledger` — the budget guard.

Every metered call increments a counter, and the cap is checked **before** the
call. The property this buys: a runaway loop costs one day's cap instead of one
month's budget. Without it, the first anyone knows is the invoice.

## Why the totals live in memory during a run

Neon's free plan bills a 5-minute idle window on every wake-up, so opening a
connection per metered call would be the most expensive possible way to run this
— see `wizcore.db.conn`. So the guard reads today's totals **once** at run
start, counts increments in memory, and flushes **once** at the end.

The cap therefore bites in memory, which is exactly where a runaway loop lives.
Cross-run accumulation comes from the opening read. The only thing this design
gives up is that a container killed mid-run loses that run's counts — the
budget then reads slightly low for the day, which fails in the safe direction
for correctness (no calls are wrongly blocked) and is why the flush also happens
on the error path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from wizcore.db.conn import connect, fetch_all

log = logging.getLogger("wizcore.db.spend")


class BudgetExceeded(RuntimeError):
    """The daily cap for a provider is spent. Stop, do not retry."""


@dataclass
class BudgetGuard:
    """Per-provider daily caps for one agent's run.

        guard = BudgetGuard.load("lead_finder", caps={"redditapis": 400, "groq": 2000})
        guard.check("redditapis")        # raises BudgetExceeded at the cap
        guard.spend("redditapis", 1, est_cost_usd=0.002)
        ...
        guard.flush()

    A provider with no cap is unlimited but still counted — you cannot decide a
    sensible cap for something you have never measured.
    """

    agent: str
    caps: dict[str, float] = field(default_factory=dict)
    _base: dict[str, float] = field(default_factory=dict)
    _delta_units: dict[str, float] = field(default_factory=dict)
    _delta_cost: dict[str, float] = field(default_factory=dict)
    url: str | None = None

    @classmethod
    def load(
        cls, agent: str, caps: dict[str, float] | None = None, url: str | None = None
    ) -> BudgetGuard:
        guard = cls(agent=agent, caps=dict(caps or {}), url=url)
        try:
            with connect(url, autocommit=True) as conn:
                for row in fetch_all(
                    conn,
                    "SELECT provider, units FROM core.spend_ledger "
                    "WHERE day = current_date AND agent = %s",
                    (agent,),
                ):
                    guard._base[row["provider"]] = float(row["units"])
        except Exception:  # noqa: BLE001
            # An unreachable ledger must not stop a run. It does mean the caps
            # only cover this run rather than the whole day, so say so loudly
            # instead of proceeding as though the guard were intact.
            log.warning("spend ledger unreadable; caps apply to THIS RUN ONLY", exc_info=True)
        return guard

    def used(self, provider: str) -> float:
        return self._base.get(provider, 0.0) + self._delta_units.get(provider, 0.0)

    def remaining(self, provider: str) -> float:
        cap = self.caps.get(provider)
        return float("inf") if cap is None else max(0.0, cap - self.used(provider))

    def check(self, provider: str, units: float = 1) -> None:
        """Raise if this call would cross the cap. Call it BEFORE the call."""
        cap = self.caps.get(provider)
        if cap is not None and self.used(provider) + units > cap:
            raise BudgetExceeded(
                f"{self.agent}: daily cap reached for {provider} "
                f"({self.used(provider):g}/{cap:g})"
            )

    def spend(self, provider: str, units: float = 1, est_cost_usd: float = 0.0) -> None:
        self._delta_units[provider] = self._delta_units.get(provider, 0.0) + units
        self._delta_cost[provider] = self._delta_cost.get(provider, 0.0) + est_cost_usd

    def afford(self, provider: str, units: float = 1, est_cost_usd: float = 0.0) -> bool:
        """check + spend, as a boolean, for call sites that skip rather than abort.

        A source that has exhausted its budget should be skipped and reported,
        not allowed to take the whole run down — one metered vendor must never
        cost you the six free ones.
        """
        try:
            self.check(provider, units)
        except BudgetExceeded as e:
            log.warning("%s", e)
            return False
        self.spend(provider, units, est_cost_usd)
        return True

    def on_llm_usage(self, provider: str = "claude_proxy"):
        """A callback for `LLMClient(on_usage=...)`.

        Tokens are counted in thousands so a cap reads as a number a human can
        reason about rather than a seven-digit one.
        """

        def _cb(model: str, usage: dict) -> None:  # noqa: ARG001
            total = float(usage.get("input_tokens") or 0) + float(usage.get("output_tokens") or 0)
            self.spend(provider, total / 1000.0)

        return _cb

    def flush(self) -> None:
        """Write the run's increments. Safe to call more than once."""
        if not self._delta_units:
            return
        rows = [
            (self.agent, provider, units, self._delta_cost.get(provider, 0.0))
            for provider, units in self._delta_units.items()
        ]
        try:
            with connect(self.url, autocommit=True) as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO core.spend_ledger (day, agent, provider, units, est_cost_usd) "
                    "VALUES (current_date, %s, %s, %s, %s) "
                    "ON CONFLICT (day, agent, provider) DO UPDATE SET "
                    "units = core.spend_ledger.units + EXCLUDED.units, "
                    "est_cost_usd = core.spend_ledger.est_cost_usd + EXCLUDED.est_cost_usd",
                    rows,
                )
            # Fold the flushed amounts into the base so a second flush() cannot
            # double-count them.
            for provider, units in self._delta_units.items():
                self._base[provider] = self._base.get(provider, 0.0) + units
            self._delta_units.clear()
            self._delta_cost.clear()
        except Exception:  # noqa: BLE001
            log.warning("could not flush spend ledger", exc_info=True)

    def summary(self) -> dict[str, str]:
        return {
            p: f"{self.used(p):g}" + (f"/{self.caps[p]:g}" if p in self.caps else "")
            for p in sorted(set(self._base) | set(self._delta_units))
        }
