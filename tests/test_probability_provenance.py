"""Probability provenance contract for closed paper trades.

`confidence` is a 0-100 heuristic score; `p_calibrated` is the calibrated win
probability. These tests pin the boundary:

  * only a value inside (0, 1] (or a 0-100 percentage that converts into it)
    is stored as a probability;
  * anything else is dropped and the `p_source` label says why;
  * the drift monitor counts coverage so "monitor is blind" is visible.
"""

import pytest

from agents.analytics.feedback_loop import (
    TradeOutcomeLogger,
    check_probability_drift,
)
from db.connection import SessionLocal
from db.models import TradeOutcome

PREFIX = "pt-probprov-"


def _cleanup() -> None:
    with SessionLocal() as s:
        s.query(TradeOutcome).filter(TradeOutcome.trade_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        s.commit()


@pytest.fixture(autouse=True)
def clean_rows():
    _cleanup()
    yield
    _cleanup()


def _log(trade_id: str, symbol: str, **kwargs):
    logger = TradeOutcomeLogger()
    return logger.log_closed_trade(
        trade_id=f"{PREFIX}{trade_id}",
        symbol=symbol,
        direction="LONG",
        signal_type="BREAKOUT",
        regime="TRENDING",
        entry_price=100.0,
        exit_price=102.0,
        pnl_pct=2.0,
        exit_reason="CLOSED_TP",
        environment="PAPER_FORWARD",
        **kwargs,
    )


def _fetch(trade_id: str) -> TradeOutcome:
    with SessionLocal() as s:
        row = (
            s.query(TradeOutcome)
            .filter(TradeOutcome.trade_id == f"{PREFIX}{trade_id}")
            .one()
        )
        s.expunge(row)
        return row


def test_probability_persisted_verbatim():
    assert _log("a", "RELIANCE_PROB", p_calibrated=0.62, p_source="ensemble_calibrator")
    row = _fetch("a")
    assert row.p_calibrated == pytest.approx(0.62)
    assert row.p_source == "ensemble_calibrator"


def test_percentage_input_is_normalised():
    assert _log("b", "TCS_PROB", p_calibrated=62.0, p_source="ensemble_pct")
    row = _fetch("b")
    assert row.p_calibrated == pytest.approx(0.62)


def test_out_of_range_value_is_rejected_not_clipped():
    assert _log("c", "INFY_PROB", p_calibrated=150.0, p_source="ensemble_bad")
    row = _fetch("c")
    assert row.p_calibrated is None
    assert row.p_source.endswith("_rejected")


def test_zero_probability_is_rejected():
    assert _log("d", "SBIN_PROB", p_calibrated=0.0, p_source="ensemble_zero")
    row = _fetch("d")
    assert row.p_calibrated is None
    assert row.p_source.endswith("_rejected")


def test_heuristic_confidence_does_not_become_probability():
    assert _log("e", "HDFC_PROB", confidence=88.0)
    row = _fetch("e")
    assert row.confidence == pytest.approx(88.0)
    assert row.p_calibrated is None
    assert row.p_source is None


def test_drift_report_exposes_probability_coverage():
    for i in range(25):
        _log(
            f"cov-{i}",
            f"COV{i}_PROB",
            confidence=70.0,
            p_calibrated=0.6,
            p_source="ensemble_calibrator",
        )

    report = check_probability_drift(lookback=200)
    assert report["status"] != "UNAVAILABLE"
    coverage = report["probability_coverage"]
    assert coverage["with_calibrated_probability"] >= 25
    assert coverage["genuine_trades_scanned"] >= 25
    # Only rows with a real probability contributed; nothing was inferred from
    # the heuristic confidence score.
    assert report["heuristic_only"] == 0
