"""
Unit tests for Gate Ablation Tracker.
Verifies that:
  1. Blocked candidates are logged as SHADOW_PENDING trades.
  2. Forward price movements resolve shadow trades to SHADOW_TP or SHADOW_SL.
  3. Ablation report accurately counts saved losses, missed wins, and net R expectancy.
"""

import pytest
from agents.analytics.gate_ablation import GateAblationTracker, ShadowBlockedTrade


@pytest.fixture
def clean_tracker(tmp_path):
    storage_file = str(tmp_path / "shadow_blocked_trades.json")
    tracker = GateAblationTracker(storage_file=storage_file)
    tracker._ensure_loaded()
    return tracker


def test_record_blocked_candidate(clean_tracker):
    trade = clean_tracker.record_blocked_candidate(
        symbol="RELIANCE",
        direction="LONG",
        entry=1000.0,
        sl=950.0,
        tp=1100.0,
        gate_reason="GATE_CORRELATION",
        regime="TRENDING",
    )
    assert trade is not None
    assert trade.status == "SHADOW_PENDING"
    assert trade.gate_reason == "GATE_CORRELATION"
    assert len(clean_tracker.shadow_trades) == 1


def test_resolve_shadow_trades_and_expectancy(clean_tracker):
    # Candidate 1: Blocked by CORRELATION -> hits SL (Gate SAVED capital!)
    clean_tracker.record_blocked_candidate(
        symbol="TCS",
        direction="LONG",
        entry=100.0,
        sl=90.0,
        tp=120.0,
        gate_reason="GATE_CORRELATION",
    )

    # Candidate 2: Blocked by COOLDOWN -> hits TP (Gate MISSED alpha!)
    clean_tracker.record_blocked_candidate(
        symbol="INFY",
        direction="LONG",
        entry=100.0,
        sl=90.0,
        tp=120.0,
        gate_reason="GATE_COOLDOWN",
    )

    # Simulate price bars: TCS drops to 88 (hits SL), INFY rises to 125 (hits TP)
    resolved = clean_tracker.resolve_shadow_trades({
        "TCS": {"high": 102.0, "low": 88.0, "close": 90.0},
        "INFY": {"high": 125.0, "low": 98.0, "close": 122.0},
    })
    assert resolved == 2

    report = clean_tracker.get_ablation_report()
    gates = report["gates"]

    assert "GATE_CORRELATION" in gates
    assert gates["GATE_CORRELATION"]["saved_losses"] == 1
    assert gates["GATE_CORRELATION"]["missed_wins"] == 0
    assert gates["GATE_CORRELATION"]["net_saved_r"] == 1.0

    assert "GATE_COOLDOWN" in gates
    assert gates["GATE_COOLDOWN"]["saved_losses"] == 0
    assert gates["GATE_COOLDOWN"]["missed_wins"] == 1
    assert gates["GATE_COOLDOWN"]["net_saved_r"] == -2.0  # Missed a 2R win!
