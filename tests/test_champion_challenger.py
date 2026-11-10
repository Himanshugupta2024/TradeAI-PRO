"""Tests for the Kronos champion/challenger promotion gate (stdlib only)."""

import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.ai_engine.champion_challenger import (  # noqa: E402
    STATE_KEY,
    ChampionChallengerLedger,
    PromotionPolicy,
    evaluate_demotion,
    evaluate_gate,
    paired_metrics,
    trailing_rows,
)


# ── fixtures ──────────────────────────────────────────────────────────────


def _policy(**overrides) -> PromotionPolicy:
    base = dict(
        min_paired_samples=20,
        min_shadow_sessions=10,
        min_expectancy_margin_pct=0.05,
        max_brier_regression=0.02,
        rolling_window=5,
        min_windows_won=0.60,
        require_calibration=True,
        max_observations=500,
    )
    base.update(overrides)
    return PromotionPolicy(**base)


def _seed_ledger(
    ledger: ChampionChallengerLedger,
    samples: int = 40,
    champion_mode: str = "bad",
    challenger_mode: str = "good",
    calibration: str = "good",
    sessions_per_day: int = 2,
) -> ChampionChallengerLedger:
    """Deterministic alternating returns; challenger can be made right/wrong."""
    start = date(2025, 1, 1)
    for i in range(samples):
        ret = 0.5 if i % 2 == 0 else -0.5
        truth = 1.0 if ret > 0 else -1.0

        if challenger_mode == "good":
            chall = truth
        elif challenger_mode == "inverse":
            chall = -truth
        else:  # flat
            chall = 0.0

        if champion_mode == "bad":
            champ = truth if i % 3 == 0 else -truth
        elif champion_mode == "same":
            champ = chall
        elif champion_mode == "good":
            champ = truth
        else:
            champ = 0.0

        if calibration == "good":
            champ_prob = 0.9 if ret > 0 else 0.1
            chall_prob = 0.9 if ret > 0 else 0.1
        elif calibration == "challenger_flat":
            champ_prob = 0.9 if ret > 0 else 0.1
            chall_prob = 0.5
        elif calibration == "none":
            champ_prob = None
            chall_prob = None
        else:
            champ_prob = None
            chall_prob = None

        ledger.record(
            symbol=f"SYM{i % sessions_per_day}",
            as_of=(start + timedelta(days=i // sessions_per_day)).isoformat(),
            champion_score=champ,
            challenger_score=chall,
            forward_return_pct=ret,
            champion_prob_up=champ_prob,
            challenger_prob_up=chall_prob,
            horizon=5,
        )
    return ledger


# ── recording / validation ────────────────────────────────────────────────


def test_rejects_invalid_observations():
    ledger = ChampionChallengerLedger(policy=_policy())
    assert ledger.record("X", "", 0.5, 0.5) is None
    assert ledger.record("X", "2025-01-02", "not-a-number", 0.5) is None
    assert ledger.record("X", "2025-01-02", 0.5, None) is None
    assert ledger.record("X", "not-a-date", 0.5, 0.5) is None
    assert ledger.observations == []


def test_scores_are_clamped_and_returns_normalized():
    ledger = ChampionChallengerLedger(policy=_policy())
    idx = ledger.record(
        "reliance",
        "2025-02-03T15:30:00+05:30",
        champion_score=7.5,
        challenger_score=-3.2,
        forward_return_pct=1.25,
        horizon=5,
    )
    assert idx == 0
    row = ledger.observations[0]
    assert row["symbol"] == "RELIANCE"
    assert row["as_of"] == "2025-02-03"
    assert row["champion_score"] == 1.0
    assert row["challenger_score"] == -1.0
    assert row["forward_return_pct"] == 1.25


def test_attach_outcome_fills_newest_pending_pair_only():
    ledger = ChampionChallengerLedger(policy=_policy())
    ledger.record("TCS", "2025-01-06", 0.4, 0.6)
    ledger.record("TCS", "2025-01-06", 0.2, 0.8)

    assert ledger.attach_outcome("tcs", "2025-01-06", 2.0) is True
    assert ledger.observations[0]["forward_return_pct"] is None
    assert ledger.observations[1]["forward_return_pct"] == 2.0
    # Newest pending pair is drained first, then the older one.
    assert ledger.attach_outcome("TCS", "2025-01-06", 3.0) is True
    assert ledger.observations[0]["forward_return_pct"] == 3.0
    assert ledger.attach_outcome("TCS", "2025-01-06", 4.0) is False


# ── metrics ───────────────────────────────────────────────────────────────


def test_metrics_are_direction_aware():
    rows = [
        {"as_of": "2025-01-01", "champion_score": -1.0, "challenger_score": 1.0, "forward_return_pct": -2.0},
        {"as_of": "2025-01-02", "champion_score": -1.0, "challenger_score": 1.0, "forward_return_pct": -1.0},
    ]
    metrics = paired_metrics(rows, window=1)
    assert metrics["champion"]["hit_rate_pct"] == 100.0
    assert metrics["champion"]["expectancy_pct"] == 1.5
    assert metrics["challenger"]["hit_rate_pct"] == 0.0
    assert metrics["challenger"]["expectancy_pct"] == -1.5
    assert metrics["expectancy_delta_pct"] == -3.0


def test_abstentions_are_excluded_not_penalized():
    rows = [
        {"as_of": "2025-01-01", "champion_score": 0.0, "challenger_score": 1.0, "forward_return_pct": 1.0},
        {"as_of": "2025-01-02", "champion_score": 0.0, "challenger_score": 0.0, "forward_return_pct": -1.0},
    ]
    metrics = paired_metrics(rows, window=2)
    assert metrics["champion"]["samples"] == 0
    assert metrics["champion"]["hit_rate_pct"] is None
    assert metrics["challenger"]["samples"] == 1
    assert metrics["challenger"]["hit_rate_pct"] == 100.0


def test_brier_requires_probabilities_on_both_sides():
    rows = [
        {
            "as_of": "2025-01-01",
            "champion_score": 1.0,
            "challenger_score": 1.0,
            "forward_return_pct": 1.0,
            "champion_prob_up": 0.6,
            "challenger_prob_up": None,
        }
    ]
    metrics = paired_metrics(rows, window=2)
    assert metrics["champion"]["brier"] == pytest.approx(0.16)
    assert metrics["challenger"]["brier"] is None


def test_pending_rows_are_not_counted_in_gate_sample():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=20)
    ledger.record("LATE", "2025-02-01", 0.5, 0.5)  # unresolved
    metrics = ledger.metrics()
    assert metrics["samples"] == 21
    assert metrics["resolved"] == 20
    assert metrics["pending"] == 1
    assert "insufficient_paired_samples" not in evaluate_gate(metrics, ledger.policy)


def test_session_count_uses_distinct_days():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=10, sessions_per_day=5)
    assert ledger.metrics()["sessions"] == 2


# ── gate ──────────────────────────────────────────────────────────────────


def test_gate_blocks_on_thin_sample():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=6)
    blockers = evaluate_gate(ledger.metrics(), ledger.policy)
    assert "insufficient_paired_samples" in blockers
    assert "insufficient_shadow_sessions" in blockers


def test_gate_blocks_when_edge_is_inside_noise():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40, champion_mode="good", challenger_mode="good")
    blockers = evaluate_gate(ledger.metrics(), ledger.policy)
    assert "expectancy_margin_not_met" in blockers
    assert "calibration_regression" not in blockers


def test_gate_blocks_on_missing_calibration():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40, calibration="none")
    blockers = evaluate_gate(ledger.metrics(), ledger.policy)
    assert "calibration_missing" in blockers

    relaxed = ChampionChallengerLedger(policy=_policy(require_calibration=False))
    _seed_ledger(relaxed, samples=40, calibration="none")
    assert "calibration_missing" not in evaluate_gate(relaxed.metrics(), relaxed.policy)


def test_gate_blocks_on_calibration_regression():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40, calibration="challenger_flat")
    metrics = ledger.metrics()
    assert metrics["brier_delta"] > ledger.policy.max_brier_regression
    assert "calibration_regression" in evaluate_gate(metrics, ledger.policy)


def test_gate_blocks_when_edge_is_not_stable_across_windows():
    metrics = {
        "resolved": 100,
        "sessions": 40,
        "champion": {"expectancy_pct": 0.10, "brier": 0.18},
        "challenger": {"expectancy_pct": 0.90, "brier": 0.17},
        "windows": {"total": 5, "won": 2},
    }
    blockers = evaluate_gate(metrics, _policy())
    assert blockers == ["edge_not_stable_across_windows"]


def test_clean_sample_passes_the_gate():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    metrics = ledger.metrics()
    assert evaluate_gate(metrics, ledger.policy) == []
    assert metrics["expectancy_delta_pct"] > 0
    assert metrics["brier_delta"] <= 0


# ── promotion / demotion lifecycle ────────────────────────────────────────


def test_recording_never_self_promotes():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    assert ledger.active == "champion"
    assert ledger.promoted_at is None


def test_explicit_evaluate_promotes_and_audits():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)

    decision = ledger.evaluate()
    assert decision.action == "promote"
    assert decision.active == "challenger"
    assert decision.promoted is True
    assert ledger.promoted_at is not None
    assert ledger.audit[-1]["action"] == "promote"

    # Second evaluation with no new evidence is a hold, not a re-promotion.
    assert ledger.evaluate().action == "hold"


def test_blocked_evaluate_keeps_champion():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40, champion_mode="good", challenger_mode="good")
    decision = ledger.evaluate()
    assert decision.action == "hold"
    assert decision.active == "champion"
    assert "expectancy_margin_not_met" in decision.blockers
    assert ledger.promoted_at is None


def test_demotion_when_trailing_edge_disappears():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    assert ledger.evaluate().action == "promote"

    # 40 fresh pairs where the challenger is systematically wrong.
    start = date(2025, 6, 1)
    for i in range(40):
        ret = 0.5 if i % 2 == 0 else -0.5
        truth = 1.0 if ret > 0 else -1.0
        ledger.record(
            symbol="SYM0",
            as_of=(start + timedelta(days=i // 2)).isoformat(),
            champion_score=truth,
            challenger_score=-truth,
            forward_return_pct=ret,
            champion_prob_up=0.9 if ret > 0 else 0.1,
            challenger_prob_up=0.9 if ret > 0 else 0.1,
        )

    decision = ledger.evaluate()
    assert decision.action == "demote"
    assert decision.active == "champion"
    assert ledger.promoted_at is None
    assert "challenger_expectancy_not_ahead" in decision.blockers


def test_active_challenger_holds_when_edge_persists():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    assert ledger.evaluate().action == "promote"

    start = date(2025, 6, 1)
    for i in range(20):
        ret = 0.5 if i % 2 == 0 else -0.5
        truth = 1.0 if ret > 0 else -1.0
        ledger.record(
            symbol="SYM0",
            as_of=(start + timedelta(days=i // 2)).isoformat(),
            champion_score=truth if i % 3 == 0 else -truth,
            challenger_score=truth,
            forward_return_pct=ret,
            champion_prob_up=0.9 if ret > 0 else 0.1,
            challenger_prob_up=0.9 if ret > 0 else 0.1,
        )

    decision = ledger.evaluate()
    assert decision.action == "hold"
    assert decision.active == "challenger"


def test_demotion_helper_ignores_missing_stats():
    metrics = {"champion": {"expectancy_pct": None}, "challenger": {"expectancy_pct": 1.0}}
    assert "no_directional_predictions" in evaluate_demotion(metrics, _policy())


def test_trailing_rows_are_bounded():
    ledger = ChampionChallengerLedger(policy=_policy(min_paired_samples=20))
    _seed_ledger(ledger, samples=100)
    rows = trailing_rows(ledger.observations, ledger.policy)
    expected = max(ledger.policy.rolling_window * 2, ledger.policy.min_paired_samples)
    assert len(rows) == expected


# ── state bounds and persistence ──────────────────────────────────────────


def test_observation_history_is_bounded():
    ledger = ChampionChallengerLedger(policy=_policy(max_observations=25))
    _seed_ledger(ledger, samples=40)
    assert len(ledger.observations) == 25
    assert ledger.observations[-1]["as_of"] >= ledger.observations[0]["as_of"]


def test_snapshot_restore_roundtrip():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    ledger.evaluate()

    clone = ChampionChallengerLedger(policy=_policy()).restore(ledger.snapshot())
    assert clone.active == "challenger"
    assert clone.promoted_at == ledger.promoted_at
    assert len(clone.observations) == len(ledger.observations)
    assert clone.metrics() == ledger.metrics()
    assert clone.audit == ledger.audit


def test_restore_drops_malformed_rows():
    raw = {
        "active": "challenger",
        "observations": [
            {"symbol": "A", "as_of": "2025-01-01", "champion_score": 0.5, "challenger_score": 0.5},
            "garbage",
            {"symbol": "B", "as_of": None, "champion_score": 0.5, "challenger_score": 0.5},
            {"symbol": "C", "as_of": "2025-01-02", "champion_score": "x", "challenger_score": 0.5},
        ],
        "audit": [{"at": "2025-01-01", "action": "promote", "reasons": []}, "junk"],
    }
    ledger = ChampionChallengerLedger(policy=_policy()).restore(raw)
    assert ledger.active == "challenger"
    assert len(ledger.observations) == 1
    assert ledger.observations[0]["symbol"] == "A"
    assert len(ledger.audit) == 1


def test_restore_of_non_dict_is_a_noop():
    ledger = ChampionChallengerLedger(policy=_policy())
    assert ledger.restore(None) is ledger
    assert ledger.active == "champion"
    assert ledger.observations == []


def test_save_and_load_use_runtime_state(monkeypatch):
    import db.runtime_state as runtime_state

    store = {}

    def fake_set(key, value):
        store[key] = value

    def fake_get(key, default=None):
        return store.get(key, default)

    monkeypatch.setattr(runtime_state, "set_runtime_state", fake_set)
    monkeypatch.setattr(runtime_state, "get_runtime_state", fake_get)

    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    ledger.evaluate()
    assert ledger.save() is True
    assert STATE_KEY in store

    reloaded = ChampionChallengerLedger(policy=_policy()).load()
    assert reloaded.active == "challenger"
    assert len(reloaded.observations) == 40


def test_save_failure_degrades_without_raising(monkeypatch):
    import db.runtime_state as runtime_state

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime_state, "set_runtime_state", boom)

    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=10)
    assert ledger.save() is False
    assert len(ledger.observations) == 10


def test_load_failure_keeps_working_in_memory(monkeypatch):
    import db.runtime_state as runtime_state

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(runtime_state, "get_runtime_state", boom)
    ledger = ChampionChallengerLedger(policy=_policy(), auto_load=True)
    assert ledger.active == "champion"
    assert ledger.record("X", "2025-01-02", 0.5, -0.5) == 0


def test_status_is_compact_and_reports_blockers():
    ledger = ChampionChallengerLedger(policy=_policy())
    _seed_ledger(ledger, samples=40)
    status = ledger.status()
    assert status["state_key"] == STATE_KEY
    assert status["active"] == "champion"
    assert status["gate"]["blockers"] == []
    assert "observations" not in status
    assert status["resolved"] == 40


def test_policy_from_settings_reads_module_knobs():
    from config import settings

    policy = PromotionPolicy.from_settings()
    assert policy.min_paired_samples == settings.CHALLENGER_MIN_PAIRED_SAMPLES
    assert policy.min_shadow_sessions == settings.CHALLENGER_MIN_SHADOW_SESSIONS
    assert policy.rolling_window == settings.CHALLENGER_ROLLING_WINDOW
