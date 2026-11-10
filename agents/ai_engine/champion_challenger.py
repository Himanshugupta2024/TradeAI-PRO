"""
Champion/challenger governance for Kronos.

Kronos is allowed to influence sizing only after it has been observed against
the incumbent signal for a documented number of sessions and has shown a real
edge on paired, like-for-like predictions. This module is the bookkeeping and
the gate for that decision. It never places orders, never imports torch, and
never reads market data.

Design constraints:

* **Import-safe.** No torch, no numpy, no pandas. ``LIGHTWEIGHT_API_MODE`` on
  the 512 MB Render service can import this freely.
* **Bounded state.** At most ``PromotionPolicy.max_observations`` resolved
  pairs are kept, so the ``runtime_state`` blob cannot grow without limit.
* **Paired only.** A champion and a challenger score are recorded on the same
  symbol / as-of / horizon. Unpaired comparisons are meaningless, so the API
  does not offer one.
* **Fail closed.** Thin sample, missing calibration, or an unstable edge across
  rolling windows all resolve to "keep the champion". Promotion is the
  exception, not the default.
* **No silent self-promotion.** Promotion and demotion both require an explicit
  ``evaluate()`` call; recording an observation never flips the active model.

State is persisted under ``kronos_champion_challenger:v1`` through
``db.runtime_state``. If the database is unavailable the ledger keeps working
in memory and logs a warning — it must not take the API down.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

STATE_KEY = "kronos_champion_challenger:v1"
STATE_VERSION = 1
MAX_AUDIT_ENTRIES = 50


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _coerce_session(value: Any) -> Optional[str]:
    """Normalise a timestamp to a ``YYYY-MM-DD`` session key."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    # Fast path: "2025-01-31T15:30:00+05:30", "2025-01-31 15:30", "2025-01-31"
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        candidate = text[:10]
        try:
            date.fromisoformat(candidate)
            return candidate
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clamp_score(value: Any) -> Optional[float]:
    number = _finite(value)
    if number is None:
        return None
    return max(-1.0, min(1.0, number))


def _prob(value: Any) -> Optional[float]:
    number = _finite(value)
    if number is None or not (0.0 <= number <= 1.0):
        return None
    return number


@dataclass(frozen=True)
class PromotionPolicy:
    """Thresholds that must all pass before a challenger replaces a champion."""

    min_paired_samples: int = 40
    min_shadow_sessions: int = 30
    min_expectancy_margin_pct: float = 0.05
    max_brier_regression: float = 0.02
    rolling_window: int = 10
    min_windows_won: float = 0.60
    require_calibration: bool = True
    max_observations: int = 2000

    @classmethod
    def from_settings(cls) -> "PromotionPolicy":
        """Build from ``config.settings`` env-backed knobs, with defaults intact."""
        try:
            from config import settings  # local import: keep module import cheap
        except Exception as exc:  # pragma: no cover - settings always present in app
            logger.debug("PromotionPolicy falling back to defaults: %s", exc)
            return cls()
        return cls(
            min_paired_samples=int(
                getattr(settings, "CHALLENGER_MIN_PAIRED_SAMPLES", cls.min_paired_samples)
            ),
            min_shadow_sessions=int(
                getattr(settings, "CHALLENGER_MIN_SHADOW_SESSIONS", cls.min_shadow_sessions)
            ),
            min_expectancy_margin_pct=float(
                getattr(
                    settings,
                    "CHALLENGER_MIN_EXPECTANCY_MARGIN_PCT",
                    cls.min_expectancy_margin_pct,
                )
            ),
            max_brier_regression=float(
                getattr(settings, "CHALLENGER_MAX_BRIER_REGRESSION", cls.max_brier_regression)
            ),
            rolling_window=int(
                getattr(settings, "CHALLENGER_ROLLING_WINDOW", cls.rolling_window)
            ),
            min_windows_won=float(
                getattr(settings, "CHALLENGER_MIN_WINDOWS_WON", cls.min_windows_won)
            ),
            require_calibration=bool(
                getattr(settings, "CHALLENGER_REQUIRE_CALIBRATION", cls.require_calibration)
            ),
            max_observations=int(
                getattr(settings, "CHALLENGER_MAX_OBSERVATIONS", cls.max_observations)
            ),
        )


@dataclass
class PromotionDecision:
    """Outcome of one gate evaluation."""

    action: str  # "hold" | "promote" | "demote"
    active: str
    blockers: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    evaluated_at: str = ""

    @property
    def promoted(self) -> bool:
        return self.active == "challenger"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "active": self.active,
            "promoted": self.promoted,
            "blockers": list(self.blockers),
            "metrics": self.metrics,
            "evaluated_at": self.evaluated_at,
        }


# ── statistics ────────────────────────────────────────────────────────────


def _directional_stats(
    rows: list[dict], score_key: str, prob_key: Optional[str] = None
) -> dict[str, Any]:
    """Signed-conviction stats for one side of the ledger.

    ``score`` in [-1, 1] is a direction, not a size. Exactly 0 is an abstention
    and is excluded from hit rate / expectancy rather than scored as a loss.
    """
    signed_returns: list[float] = []
    hits = 0
    for row in rows:
        score = row.get(score_key)
        ret = row.get("forward_return_pct")
        if score is None or ret is None:
            continue
        if score > 0:
            direction = 1.0
        elif score < 0:
            direction = -1.0
        else:
            continue
        signed = direction * float(ret)
        signed_returns.append(signed)
        if signed > 0:
            hits += 1

    n = len(signed_returns)
    return {
        "samples": n,
        "hit_rate_pct": round(hits / n * 100.0, 2) if n else None,
        "expectancy_pct": round(sum(signed_returns) / n, 4) if n else None,
        "brier": _brier(rows, prob_key) if prob_key else None,
    }


def _brier(rows: list[dict], prob_key: str) -> Optional[float]:
    errors: list[float] = []
    for row in rows:
        prob = row.get(prob_key)
        ret = row.get("forward_return_pct")
        if prob is None or ret is None:
            continue
        outcome = 1.0 if float(ret) > 0 else 0.0
        errors.append((float(prob) - outcome) ** 2)
    if not errors:
        return None
    return round(sum(errors) / len(errors), 4)


def _rolling_window_wins(rows: list[dict], window: int) -> dict[str, int]:
    """Count consecutive non-overlapping windows where the challenger wins."""
    usable = [
        row
        for row in rows
        if row.get("forward_return_pct") is not None
        and row.get("champion_score") is not None
        and row.get("challenger_score") is not None
    ]
    if window < 2 or len(usable) < window:
        return {"total": 0, "won": 0}

    won = 0
    total = 0
    for start in range(0, len(usable) - window + 1, window):
        chunk = usable[start : start + window]
        champ = _directional_stats(chunk, "champion_score")["expectancy_pct"]
        chall = _directional_stats(chunk, "challenger_score")["expectancy_pct"]
        if champ is None or chall is None:
            continue
        total += 1
        if chall > champ:
            won += 1
    return {"total": total, "won": won}


def paired_metrics(observations: Iterable[dict], window: int = 10) -> dict[str, Any]:
    """Paired champion-vs-challenger metrics over the resolved observations."""
    rows = list(observations)
    resolved = [row for row in rows if row.get("forward_return_pct") is not None]
    sessions = {
        session
        for row in resolved
        for session in [_coerce_session(row.get("as_of"))]
        if session
    }
    champion = _directional_stats(resolved, "champion_score", "champion_prob_up")
    challenger = _directional_stats(resolved, "challenger_score", "challenger_prob_up")
    windows = _rolling_window_wins(resolved, window)

    delta = None
    if champion["expectancy_pct"] is not None and challenger["expectancy_pct"] is not None:
        delta = round(challenger["expectancy_pct"] - champion["expectancy_pct"], 4)

    brier_delta = None
    if champion["brier"] is not None and challenger["brier"] is not None:
        brier_delta = round(challenger["brier"] - champion["brier"], 4)

    return {
        "samples": len(rows),
        "resolved": len(resolved),
        "pending": len(rows) - len(resolved),
        "sessions": len(sessions),
        "champion": champion,
        "challenger": challenger,
        "expectancy_delta_pct": delta,
        "brier_delta": brier_delta,
        "windows": windows,
    }


def evaluate_gate(metrics: dict[str, Any], policy: PromotionPolicy) -> list[str]:
    """Return the blocker list. Empty list means promotion is warranted."""
    blockers: list[str] = []

    if metrics.get("resolved", 0) < policy.min_paired_samples:
        blockers.append("insufficient_paired_samples")
    if metrics.get("sessions", 0) < policy.min_shadow_sessions:
        blockers.append("insufficient_shadow_sessions")

    champion = metrics.get("champion") or {}
    challenger = metrics.get("challenger") or {}
    champ_exp = champion.get("expectancy_pct")
    chall_exp = challenger.get("expectancy_pct")
    if champ_exp is None or chall_exp is None:
        blockers.append("no_directional_predictions")
    elif chall_exp - champ_exp <= policy.min_expectancy_margin_pct:
        blockers.append("expectancy_margin_not_met")

    if policy.require_calibration:
        champ_brier = champion.get("brier")
        chall_brier = challenger.get("brier")
        if champ_brier is None or chall_brier is None:
            blockers.append("calibration_missing")
        elif chall_brier - champ_brier > policy.max_brier_regression:
            blockers.append("calibration_regression")

    windows = metrics.get("windows") or {}
    total_windows = int(windows.get("total", 0) or 0)
    won_windows = int(windows.get("won", 0) or 0)
    if total_windows < 2:
        blockers.append("insufficient_evaluation_windows")
    elif won_windows / total_windows < policy.min_windows_won:
        blockers.append("edge_not_stable_across_windows")

    return blockers


def evaluate_demotion(metrics: dict[str, Any], policy: PromotionPolicy) -> list[str]:
    """Return reasons to demote an *already active* challenger.

    Uses a trailing slice so a stale promotion cannot coast on an old edge.
    """
    reasons: list[str] = []
    champion = metrics.get("champion") or {}
    challenger = metrics.get("challenger") or {}
    champ_exp = champion.get("expectancy_pct")
    chall_exp = challenger.get("expectancy_pct")
    if champ_exp is None or chall_exp is None:
        reasons.append("no_directional_predictions")
    elif chall_exp <= champ_exp:
        reasons.append("challenger_expectancy_not_ahead")

    champ_brier = champion.get("brier")
    chall_brier = challenger.get("brier")
    if champ_brier is not None and chall_brier is not None:
        if chall_brier - champ_brier > policy.max_brier_regression:
            reasons.append("calibration_regression")
    return reasons


def trailing_rows(observations: Iterable[dict], policy: PromotionPolicy) -> list[dict]:
    """Resolved rows used for demotion checks: the newest trailing sample."""
    resolved = [row for row in observations if row.get("forward_return_pct") is not None]
    if not resolved:
        return []
    limit = max(policy.rolling_window * 2, policy.min_paired_samples)
    return resolved[-limit:]


# ── ledger ────────────────────────────────────────────────────────────────


class ChampionChallengerLedger:
    """Paired observation store plus the promotion/demotion gate."""

    def __init__(
        self,
        policy: Optional[PromotionPolicy] = None,
        champion: str = "incumbent",
        challenger: str = "kronos",
        auto_load: bool = False,
        state_key: Optional[str] = None,
    ):
        self.policy = policy or PromotionPolicy.from_settings()
        self.champion = champion
        self.challenger = challenger
        self.state_key = state_key or STATE_KEY
        self.observations: list[dict] = []
        self.active = "champion"
        self.promoted_at: Optional[str] = None
        self.last_evaluated_at: Optional[str] = None
        self.audit: list[dict] = []
        self._persisted = False
        if auto_load:
            self.load()

    # ── recording ─────────────────────────────────────────────────────────
    def record(
        self,
        symbol: str,
        as_of: Any,
        champion_score: Any,
        challenger_score: Any,
        forward_return_pct: Any = None,
        champion_prob_up: Any = None,
        challenger_prob_up: Any = None,
        horizon: int = 0,
    ) -> Optional[int]:
        """Append one paired observation. Returns its index, or None if invalid."""
        session = _coerce_session(as_of)
        champ = _clamp_score(champion_score)
        chall = _clamp_score(challenger_score)
        if session is None or champ is None or chall is None:
            logger.debug(
                "[Challenger] rejected observation %s %s (as_of=%r, champ=%r, chall=%r)",
                symbol,
                horizon,
                as_of,
                champion_score,
                challenger_score,
            )
            return None

        ret = _finite(forward_return_pct)
        champ_prob = _prob(champion_prob_up)
        chall_prob = _prob(challenger_prob_up)

        self.observations.append(
            {
                "symbol": str(symbol).upper(),
                "as_of": session,
                "horizon": int(horizon or 0),
                "champion_score": round(champ, 6),
                "challenger_score": round(chall, 6),
                "champion_prob_up": champ_prob,
                "challenger_prob_up": chall_prob,
                "forward_return_pct": ret,
                "recorded_at": _utc_now_iso(),
            }
        )
        self._trim()
        return len(self.observations) - 1

    def attach_outcome(self, symbol: str, as_of: Any, forward_return_pct: Any) -> bool:
        """Fill the newest unresolved pair for (symbol, session)."""
        session = _coerce_session(as_of)
        ret = _finite(forward_return_pct)
        if session is None or ret is None:
            return False
        target = str(symbol).upper()
        for row in reversed(self.observations):
            if (
                row.get("symbol") == target
                and row.get("as_of") == session
                and row.get("forward_return_pct") is None
            ):
                row["forward_return_pct"] = ret
                return True
        return False

    def _trim(self) -> None:
        limit = max(1, int(self.policy.max_observations))
        if len(self.observations) > limit:
            self.observations = self.observations[-limit:]

    # ── metrics and gate ──────────────────────────────────────────────────
    def metrics(self) -> dict[str, Any]:
        return paired_metrics(self.observations, window=self.policy.rolling_window)

    def evaluate(self) -> PromotionDecision:
        """Run the gate once and apply the resulting state transition."""
        metrics = self.metrics()
        now = _utc_now_iso()
        self.last_evaluated_at = now

        if self.active == "challenger":
            reasons = evaluate_demotion(
                paired_metrics(trailing_rows(self.observations, self.policy), self.policy.rolling_window),
                self.policy,
            )
            if reasons:
                self.active = "champion"
                self.promoted_at = None
                self._audit(now, "demote", reasons)
                return PromotionDecision(
                    action="demote",
                    active=self.active,
                    blockers=reasons,
                    metrics=metrics,
                    evaluated_at=now,
                )
            return PromotionDecision(
                action="hold", active=self.active, metrics=metrics, evaluated_at=now
            )

        blockers = evaluate_gate(metrics, self.policy)
        if blockers:
            return PromotionDecision(
                action="hold",
                active=self.active,
                blockers=blockers,
                metrics=metrics,
                evaluated_at=now,
            )

        self.active = "challenger"
        self.promoted_at = now
        self._audit(now, "promote", [])
        return PromotionDecision(
            action="promote", active=self.active, metrics=metrics, evaluated_at=now
        )

    def _audit(self, at: str, action: str, reasons: list[str]) -> None:
        self.audit.append({"at": at, "action": action, "reasons": list(reasons)})
        if len(self.audit) > MAX_AUDIT_ENTRIES:
            self.audit = self.audit[-MAX_AUDIT_ENTRIES:]

    def status(self) -> dict[str, Any]:
        """Compact summary for the dashboard / API — no raw observation dump."""
        metrics = self.metrics()
        policy = self.policy
        return {
            "state_key": self.state_key,
            "active": self.active,
            "promoted": self.active == "challenger",
            "promoted_at": self.promoted_at,
            "last_evaluated_at": self.last_evaluated_at,
            "champion": self.champion,
            "challenger": self.challenger,
            "samples": metrics["samples"],
            "resolved": metrics["resolved"],
            "pending": metrics["pending"],
            "sessions": metrics["sessions"],
            "champion_stats": metrics["champion"],
            "challenger_stats": metrics["challenger"],
            "expectancy_delta_pct": metrics["expectancy_delta_pct"],
            "brier_delta": metrics["brier_delta"],
            "windows": metrics["windows"],
            "gate": {
                "min_paired_samples": policy.min_paired_samples,
                "min_shadow_sessions": policy.min_shadow_sessions,
                "min_expectancy_margin_pct": policy.min_expectancy_margin_pct,
                "max_brier_regression": policy.max_brier_regression,
                "min_windows_won": policy.min_windows_won,
                "require_calibration": policy.require_calibration,
                "blockers": evaluate_gate(metrics, policy),
            },
            "audit_tail": self.audit[-5:],
        }

    # ── persistence ───────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "active": self.active,
            "champion": self.champion,
            "challenger": self.challenger,
            "promoted_at": self.promoted_at,
            "last_evaluated_at": self.last_evaluated_at,
            "observations": self.observations,
            "audit": self.audit,
        }

    def restore(self, raw: Any) -> "ChampionChallengerLedger":
        """Load a persisted snapshot defensively; malformed rows are dropped."""
        if not isinstance(raw, dict):
            return self
        self.active = "challenger" if raw.get("active") == "challenger" else "champion"
        self.champion = str(raw.get("champion") or self.champion)
        self.challenger = str(raw.get("challenger") or self.challenger)
        self.promoted_at = raw.get("promoted_at") if isinstance(raw.get("promoted_at"), str) else None
        self.last_evaluated_at = (
            raw.get("last_evaluated_at")
            if isinstance(raw.get("last_evaluated_at"), str)
            else None
        )

        rows = raw.get("observations")
        restored: list[dict] = []
        if isinstance(rows, list):
            for row in rows:
                clean = self._clean_row(row)
                if clean is not None:
                    restored.append(clean)
        self.observations = restored
        self._trim()

        audit = raw.get("audit")
        if isinstance(audit, list):
            self.audit = [entry for entry in audit if isinstance(entry, dict)][-MAX_AUDIT_ENTRIES:]
        else:
            self.audit = []
        return self

    @staticmethod
    def _clean_row(row: Any) -> Optional[dict]:
        if not isinstance(row, dict):
            return None
        session = _coerce_session(row.get("as_of"))
        champ = _clamp_score(row.get("champion_score"))
        chall = _clamp_score(row.get("challenger_score"))
        if session is None or champ is None or chall is None:
            return None
        return {
            "symbol": str(row.get("symbol") or "").upper(),
            "as_of": session,
            "horizon": int(row.get("horizon") or 0),
            "champion_score": champ,
            "challenger_score": chall,
            "champion_prob_up": _prob(row.get("champion_prob_up")),
            "challenger_prob_up": _prob(row.get("challenger_prob_up")),
            "forward_return_pct": _finite(row.get("forward_return_pct")),
            "recorded_at": row.get("recorded_at") if isinstance(row.get("recorded_at"), str) else None,
        }

    def save(self) -> bool:
        """Persist to runtime state. Returns False when the DB is unavailable."""
        try:
            from db.runtime_state import set_runtime_state

            set_runtime_state(self.state_key, self.snapshot())
            self._persisted = True
            return True
        except Exception as exc:
            self._persisted = False
            logger.warning("[Challenger] could not persist ledger: %s", exc)
            return False

    def load(self) -> "ChampionChallengerLedger":
        try:
            from db.runtime_state import get_runtime_state

            raw = get_runtime_state(self.state_key, None)
        except Exception as exc:
            logger.warning("[Challenger] could not load ledger: %s", exc)
            self._persisted = False
            return self
        if raw is None:
            self._persisted = True  # readable DB, no prior state
            return self
        self.restore(raw)
        self._persisted = True
        return self

    def reset(self) -> None:
        self.observations = []
        self.active = "champion"
        self.promoted_at = None
        self.last_evaluated_at = None
        self.audit = []


_LEDGER: Optional[ChampionChallengerLedger] = None


def get_ledger(policy: Optional[PromotionPolicy] = None) -> ChampionChallengerLedger:
    """Process-wide ledger, loaded from runtime state on first use."""
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = ChampionChallengerLedger(policy=policy, auto_load=True)
    return _LEDGER


def champion_challenger_status() -> dict[str, Any]:
    """Dashboard-friendly status; degrades to an error dict instead of raising."""
    try:
        return get_ledger().status()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[Challenger] status unavailable: %s", exc)
        return {"error": str(exc), "active": "champion", "state_key": STATE_KEY}
