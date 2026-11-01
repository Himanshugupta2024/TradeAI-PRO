import pandas as pd

from agents.market_intelligence.swing_scanner import scan_swing_setups
from config import settings
from risk.swing_risk import (
    calculate_swing_position_size,
    calculate_swing_sl_tp,
    overnight_risk_check,
    update_trailing_sl,
)


def _minimal_daily_df(*, n: int = 50) -> pd.DataFrame:
    end = pd.Timestamp.now().normalize()
    dates = pd.date_range(end=end, periods=n, freq="D")
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 200_000.0,
            "RSI_14": 52.0,
            "EMA_20": 100.0,
            "EMA_50": 99.0,
            "ATR_14": 1.2,
            "ADX_14": 24.0,
        }
    )


def test_calculate_swing_sl_tp_long_and_short(monkeypatch):
    monkeypatch.setattr(settings, "SWING_ATR_SL_MULTIPLIER", 2.0)
    monkeypatch.setattr(settings, "SWING_ATR_TARGET_MULTIPLIER", 3.0)

    long_levels = calculate_swing_sl_tp(entry=100.0, atr=2.0, direction="LONG")
    short_levels = calculate_swing_sl_tp(entry=100.0, atr=2.0, direction="SHORT")

    assert long_levels["stop"] == 96.0
    assert long_levels["target"] == 106.0
    assert short_levels["stop"] == 104.0
    assert short_levels["target"] == 94.0
    assert long_levels["risk_reward"] == 1.5
    assert short_levels["risk_reward"] == 1.5


def test_calculate_swing_position_size_uses_risk_fraction():
    out = calculate_swing_position_size(
        capital=100_000.0,
        entry=500.0,
        stop=490.0,
        risk_pct=0.015,
    )
    # risk amount = 1500, risk/share=10 => qty=150 (capital cap also allows this)
    assert out["qty"] == 150
    assert out["risk_amount"] == 1500.0
    assert out["capital_used"] == 75_000.0


def test_overnight_risk_check_reduces_qty_for_capital_limit(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_CAPITAL_PER_TRADE", 0.25)
    ok, reason, qty = overnight_risk_check(
        qty=100,
        entry=1000.0,
        capital=100_000.0,
        open_trades=[],
    )
    assert ok is True
    assert qty == 25
    assert "Reduced qty" in reason


def test_overnight_risk_check_blocks_when_open_positions_limit(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_OPEN_POSITIONS", 2)
    open_trades = [
        {"entry_price": 100.0, "stop_loss": 95.0, "quantity": 10},
        {"entry_price": 200.0, "stop_loss": 190.0, "quantity": 5},
    ]
    ok, reason, qty = overnight_risk_check(
        qty=10,
        entry=100.0,
        capital=100_000.0,
        open_trades=open_trades,
    )
    assert ok is False
    assert qty == 0
    assert "Max swing positions" in reason


def test_update_trailing_sl_moves_to_breakeven_then_locks_gain():
    be = update_trailing_sl(
        entry=100.0,
        current_price=102.5,
        atr=2.0,
        original_sl=96.0,
        direction="LONG",
    )
    lock = update_trailing_sl(
        entry=100.0,
        current_price=104.5,
        atr=2.0,
        original_sl=96.0,
        direction="LONG",
    )
    assert be["new_sl"] == 100.0
    assert "breakeven" in be["action"].lower()
    assert lock["new_sl"] == 101.0
    assert "lock" in lock["action"].lower()


def test_scan_swing_setups_populates_execution_risk_fields(monkeypatch):
    monkeypatch.setattr(settings, "SWING_MAX_BAR_AGE_DAYS", 365.0)
    df = _minimal_daily_df()

    out = scan_swing_setups(
        df,
        symbol="TESTSYM",
        allow_fallback=True,
        capital=100_000.0,
        open_trades=[],
    )

    assert out
    s = out[0]
    assert s.entry == s.entry_zone
    assert s.stop == s.stop_loss
    assert s.target == s.target_1
    assert s.atr > 0
    assert s.qty >= 0
    assert s.risk_amount >= 0
    assert s.capital_used >= 0
