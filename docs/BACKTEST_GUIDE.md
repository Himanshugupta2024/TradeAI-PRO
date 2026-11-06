# Backtesting Guide — Costs and Portfolio Simulation

Two changes were made to how TradeAI PRO evaluates a strategy:

1. **Transaction costs are now explicit and realistic**, and slippage is charged once.
2. **A portfolio layer exists**, so per-symbol signals are replayed through one account instead of each symbol assuming the full capital.

Both are deliberately conservative. Results will look worse than before — that is the point.

---

## 1. Cost model (`agents/backtester/costs.py`)

| | Delivery (CNC) | Intraday (MIS) |
|---|---|---|
| Brokerage | 3 bps / side | 2 bps / side |
| STT | 10 bps buy + 10 bps sell | 0 buy + 2.5 bps sell |
| Exchange | 0.3 bps / side | 0.3 bps / side |
| SEBI | 0.01 bps / side | 0.01 bps / side |
| Stamp | 1.5 bps buy | 0.3 bps buy |
| GST | 18% on brokerage + exchange + SEBI | same |
| **Round trip (ex-slippage)** | **~29 bps** | **~8 bps** |
| Slippage | 5 bps / side | 5 bps / side |
| **Effective round trip** | **~39 bps** | **~18 bps** |

GST is applied to the taxable components only — not to STT or stamp duty.

### What changed in the engine

- `Backtester(cost_profile="delivery")` is the new default. `cost_profile="intraday"` for 15m bars.
- `cost_bps=` still works as a legacy flat round-trip override.
- The old formula charged slippage twice: once in the fill price and again inside `costs_inr`. It is now charged once, in the price, and `CostModel` adds only statutory charges.
- `cost_assumptions` on the result now reports the profile, per-leg bps, and effective round trip.

### Impact

A delivery strategy trading a 3% move used to pay ~0.10%; it now pays ~0.39%. Strategies with small average wins will flip from positive to negative. That is the correct outcome.

---

## 2. Portfolio simulator (`agents/backtester/portfolio.py`)

The per-symbol engine answers "does this signal have an edge on one chart?". `PortfolioBacktester` answers "what would this account have done?".

It enforces:

- one shared cash balance
- risk-based sizing: `risk_per_trade_pct` of current equity, stop-distance based
- `max_positions` concurrent positions
- `max_position_pct` notional cap per position
- `sector_cap_pct` per sector
- one open position per symbol
- commission/STT/exchange/stamp costs on every round trip

```python
from agents.backtester import (
    Backtester,
    PortfolioBacktester,
    portfolio_trades_from_backtest,
)

# 1. Per-symbol edge
bt = Backtester(cost_profile="delivery", slippage_pct=0.05)
result = bt.run(df_with_signals)

# 2. Feed candidates into one account
candidates = portfolio_trades_from_backtest("RELIANCE", result, sector="ENERGY")
portfolio = PortfolioBacktester(
    initial_capital=100_000,
    max_positions=8,
    risk_per_trade_pct=0.75,
    max_position_pct=15.0,
    sector_cap_pct=30.0,
).run(candidates)

print(portfolio.summary())
```

`PortfolioResult` reports: final equity, total return, CAGR, max drawdown, Sharpe, win rate, profit factor, average exposure, skipped counts by reason (`max_positions`, `sector_cap`, `capital`, `already_open`), the full trade log, and the equity curve.

---

## 3. What still needs work

| Gap | Effect | Next step |
|---|---|---|
| No point-in-time universe | Survivorship bias inflates returns | Load historical index constituents, or restrict to a fixed list and state it |
| No corporate-action adjustment in the engine | Split/bonus days create fake gaps | Adjust OHLC before backtesting; `agents/data_ingestion/corporate_events.py` already fetches events |
| Single constant slippage | Small caps are more expensive than large caps | Bucket by median daily turnover |
| Sharpe from trade returns in the single-symbol engine | Not comparable across strategies | Use the portfolio equity curve (this module) instead |
| No live/backtest parity check | Paper fills may differ from simulated | Compare shadow trades against the same signals |

---

## 4. Test coverage

`tests/test_cost_model.py` and `tests/test_portfolio_backtester.py` cover:

- profile bps are in the expected range and intraday < delivery
- GST is not applied to STT/stamp
- engine cost equals `CostModel.trade_cost` (no double count)
- delivery costs exceed the old 10 bps flat assumption
- max positions, sector cap, duplicate symbol, empty input
- equity change equals the sum of net P&L
- position size respects the risk budget

Run with:

```bash
pytest -m "not integration" -q
```
