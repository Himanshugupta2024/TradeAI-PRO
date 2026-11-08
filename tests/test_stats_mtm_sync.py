import pytest
from agents.execution.paper_trading import PaperTradingEngine, PaperTrade


def test_paper_trading_portfolio_equity_with_current_prices():
    engine = PaperTradingEngine(initial_capital=100000.0)
    engine.capital = 98626.13

    # Add 2 open positions matching IRCTC & DRREDDY
    trade1 = PaperTrade(
        id=31,
        symbol="IRCTC",
        direction="LONG",
        entry_price=447.50,
        stop_loss=433.69,
        target=496.44,
        entry_time="2026-11-06T10:00:00+05:30",
        quantity=55.9,
    )
    trade1.quote_currency = "INR"
    trade1.base_currency = "INR"
    trade1.fx_rate_entry = 1.0

    trade2 = PaperTrade(
        id=32,
        symbol="DRREDDY",
        direction="LONG",
        entry_price=1368.30,
        stop_loss=1313.09,
        target=1644.38,
        entry_time="2026-11-06T10:00:00+05:30",
        quantity=18.3,
    )
    trade2.quote_currency = "INR"
    trade2.base_currency = "INR"
    trade2.fx_rate_entry = 1.0

    engine.open_trades = [trade1, trade2]

    # Test with current_prices dictionary
    prices = {
        "IRCTC": {"close": 464.45},
        "DRREDDY": {"close": 1370.00},
    }

    stats = engine.get_stats(current_prices=prices)

    assert stats["unrealized_pnl"] > 900.0
    assert stats["capital"] == engine.capital
    assert stats["total_equity"] > 99500.0
    assert abs(stats["total_equity"] - (stats["capital"] + stats["unrealized_pnl"])) < 0.01


def test_paper_trading_portfolio_equity_trade_current_price_fallback():
    engine = PaperTradingEngine(initial_capital=100000.0)
    engine.capital = 98626.13

    trade = PaperTrade(
        id=31,
        symbol="IRCTC",
        direction="LONG",
        entry_price=447.50,
        stop_loss=433.69,
        target=496.44,
        entry_time="2026-11-06T10:00:00+05:30",
        quantity=55.9,
    )
    trade.current_price = 464.45
    trade.quote_currency = "INR"
    trade.base_currency = "INR"
    trade.fx_rate_entry = 1.0

    engine.open_trades = [trade]

    # Without current_prices, uses trade.current_price fallback
    stats = engine.get_stats()
    assert stats["unrealized_pnl"] > 900.0
