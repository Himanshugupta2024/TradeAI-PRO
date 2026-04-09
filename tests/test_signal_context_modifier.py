"""Tests for signal context modifier rules in signal_generator."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents.signal_engine.signal_generator import _apply_context_rules


def test_event_risk_reduces_confidence():
    """Event risk should reduce directional confidence by 25%."""
    conf, adjusted, warning, reasons = _apply_context_rules(
        direction="LONG",
        confidence=80.0,
        context_score=0.0,
        event_risk_active=True,
        news_volume_spike=False,
        btc_dominance_7d_trend="FLAT",
        symbol="BTCUSDT",
    )
    assert conf == 60.0
    assert adjusted is True
    assert warning is None
    assert "event_risk_active" in reasons


def test_negative_context_reduces_long():
    """Negative context below threshold should reduce LONG by 20%."""
    conf, adjusted, _, reasons = _apply_context_rules(
        direction="LONG",
        confidence=70.0,
        context_score=-0.5,
        event_risk_active=False,
        news_volume_spike=False,
        btc_dominance_7d_trend="FLAT",
        symbol="BTCUSDT",
    )
    assert conf == 56.0
    assert adjusted is True
    assert "negative_context_vs_long" in reasons


def test_positive_context_boosts_long():
    """Strong positive context should boost LONG confidence up to 95 cap."""
    conf, adjusted, _, reasons = _apply_context_rules(
        direction="LONG",
        confidence=90.0,
        context_score=0.6,
        event_risk_active=False,
        news_volume_spike=False,
        btc_dominance_7d_trend="FLAT",
        symbol="BTCUSDT",
    )
    assert conf == 95.0
    assert adjusted is True
    assert "strong_positive_context_long" in reasons


def test_btc_dominance_reduces_altcoin_long():
    """Altcoin LONG must be dampened when BTC dominance is rising."""
    conf, adjusted, _, reasons = _apply_context_rules(
        direction="LONG",
        confidence=80.0,
        context_score=0.1,
        event_risk_active=False,
        news_volume_spike=False,
        btc_dominance_7d_trend="RISING",
        symbol="ETHUSDT",
    )
    assert conf == 68.0
    assert adjusted is True
    assert "btc_dominance_rising_altcoin_long" in reasons


def test_no_modification_on_skip_signal():
    """SKIP should not be confidence-modified by context rules."""
    conf, adjusted, warning, reasons = _apply_context_rules(
        direction="SKIP",
        confidence=50.0,
        context_score=-0.8,
        event_risk_active=True,
        news_volume_spike=True,
        btc_dominance_7d_trend="RISING",
        symbol="SOLUSDT",
    )
    assert conf == 50.0
    assert adjusted is False
    assert warning is None
    assert reasons == []
