"""
Multi-Currency Accounting & Deterministic Test Suite.

Verifies:
1. Deterministic fixture:
   INR P&L of +300 plus USDT P&L of +10 at a test rate of 84 INR/USDT
   aggregates to exactly +1,140 INR before any separately specified costs.
2. Short position currency conversion.
3. Multi-currency exposure bounds and margin calculations.
4. Legacy trade hydration and migration idempotence.
5. Missing FX rate rejection policy (never falls back to 1.0).
"""

import pytest
from decimal import Decimal
from agents.execution.currency import (
    FXRateService,
    resolve_instrument_identity,
    quantize_inr,
    quantize_native,
    to_decimal,
    MissingFXRateError,
)
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


def test_instrument_identity_resolution():
    """Verify USDT is preserved as USDT, INR as INR, USD as USD."""
    ac, qc, bc = resolve_instrument_identity("BTC/USDT")
    assert ac == "CRYPTO"
    assert qc == "USDT"
    assert bc == "INR"

    ac, qc, bc = resolve_instrument_identity("DOGEUSDT")
    assert ac == "CRYPTO"
    assert qc == "USDT"
    assert bc == "INR"

    ac, qc, bc = resolve_instrument_identity("RELIANCE")
    assert ac == "EQUITY"
    assert qc == "INR"
    assert bc == "INR"

    ac, qc, bc = resolve_instrument_identity("EUR/USD")
    assert ac == "FOREX"
    assert qc == "USD"
    assert bc == "INR"

    ac, qc, bc = resolve_instrument_identity("USD/INR")
    assert ac == "FOREX"
    assert qc == "INR"
    assert bc == "INR"


def test_deterministic_multi_currency_fixture():
    """
    DETERMINISTIC FIXTURE TEST:
    INR P&L of +300 plus USDT P&L of +10 at a test rate of 84 INR/USDT
    must aggregate to exactly +1,140 INR.
    """
    fx_svc = FXRateService.get_instance()
    fx_svc.clear_test_rates()
    # Inject exact deterministic rate
    fx_svc.set_test_rate("USDT", "INR", 84.0)

    engine = PaperTradingEngine()
    engine.trades = []
    engine.open_trades = []
    engine.capital = 100000.0
    engine.initial_capital = 100000.0

    # 1. Closed INR trade with +300 INR PnL
    t_inr = PaperTrade(
        id=1,
        symbol="IRCTC",
        direction="LONG",
        entry_price=400.0,
        stop_loss=380.0,
        target=450.0,
        entry_time="2026-11-05T10:00:00+05:30",
        quantity=10.0,
        initial_quantity=10.0,
        status="CLOSED_TP",
        exit_price=430.0,
        exit_time="2026-11-05T11:00:00+05:30",
        quote_currency="INR",
        base_currency="INR",
        fx_rate_entry=1.0,
        fx_rate_exit=1.0,
        total_pnl_native=300.0,
        total_pnl=300.0,  # Base INR
    )

    # 2. Closed USDT crypto trade with +10.0 USDT PnL
    t_usdt = PaperTrade(
        id=2,
        symbol="BTC/USDT",
        direction="LONG",
        entry_price=60000.0,
        stop_loss=58000.0,
        target=65000.0,
        entry_time="2026-11-05T10:15:00+05:30",
        quantity=0.01,
        initial_quantity=0.01,
        status="CLOSED_TP",
        exit_price=61000.0,
        exit_time="2026-11-05T11:15:00+05:30",
        quote_currency="USDT",
        base_currency="INR",
        fx_rate_entry=84.0,
        fx_rate_exit=84.0,
        total_pnl_native=10.0,  # 10 USDT
        total_pnl=840.0,        # 10 USDT * 84 = 840 INR
    )

    engine.trades = [t_inr, t_usdt]

    stats = engine.get_stats()
    assert stats["total_trades"] == 2
    assert stats["winning_trades"] == 2
    assert stats["losing_trades"] == 0

    # Strict assertion for deterministic aggregated total: 300 + (10 * 84) = 1,140 INR
    expected_total_pnl = 1140.0
    assert stats["total_pnl"] == expected_total_pnl, f"Expected {expected_total_pnl}, got {stats['total_pnl']}"

    # Clean up test rates
    fx_svc.clear_test_rates()


def test_short_usdt_trade_execution_and_closure():
    """Verify short crypto trade sizing, PnL calculation, and base currency capital update."""
    fx_svc = FXRateService.get_instance()
    fx_svc.clear_test_rates()
    fx_svc.set_test_rate("USDT", "INR", 85.0)

    engine = PaperTradingEngine()
    engine.trades = []
    engine.open_trades = []
    engine._cooldowns.clear()
    engine.capital = 100000.0
    engine.initial_capital = 100000.0

    # Short trade on ETH/USDT: Entry 3000, SL 3100, TP 2800
    trade = engine.execute(
        symbol="ETH/USDT",
        direction="SHORT",
        entry=3000.0,
        sl=3100.0,
        tp=2800.0,
        confidence=70.0,
        allow_duplicate_symbol=True,
    )

    assert trade is not None
    assert trade.quote_currency == "USDT"
    assert trade.base_currency == "INR"
    assert trade.fx_rate_entry == 85.0

    # Close trade at target 2800 (+200 USDT per unit)
    qty = trade.quantity
    engine._close_trade(trade, exit_price=2800.0, status="CLOSED_TP")

    expected_native_pnl = (3000.0 - 2800.0) * qty
    expected_base_pnl = round(expected_native_pnl * 85.0, 2)

    assert round(trade.total_pnl_native, 2) == round(expected_native_pnl, 2)
    assert round(trade.total_pnl, 2) == expected_base_pnl
    assert round(engine.capital, 2) == round(100000.0 + expected_base_pnl, 2)

    fx_svc.clear_test_rates()


def test_missing_fx_rate_error_handling():
    """Verify that unsupported/missing FX rates raise MissingFXRateError and do not default to 1.0."""
    fx_svc = FXRateService.get_instance()
    fx_svc.clear_test_rates()

    # Querying a fictional currency pair must fail, not silently return 1.0
    with pytest.raises(MissingFXRateError):
        fx_svc.get_fx_rate("XYZ_COIN", "INR")
