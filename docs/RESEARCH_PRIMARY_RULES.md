# Primary-Rule Research — NIFTY Large Caps, 2025-01 → 2026-11

**Question:** does the primary signal have any gross edge before we invest in a meta-model, Kronos features, or portfolio optimisation?

**Answer for the rules tested: no.** Every configuration is negative after costs, and most are negative before them.

## Method

| Item | Value |
|---|---|
| Universe | 39 current NIFTY large caps (Yahoo `.NS` daily bars) |
| Window | 2025-01-01 → 2026-11-05 |
| Bars | 17,862 |
| Labels | triple barrier, ATR-scaled, stop wins a same-bar touch |
| Vertical barrier | 10 bars |
| Cost | 0.39% round trip (delivery profile) |
| Signal spacing | minimum 10 bars between signals (≈ independent observations) |
| Split | chronological 70/15/15 |

Data source is Yahoo, not the official bhavcopy: prices are split-adjusted but the universe is today's constituents. Treat this as a fast first read, not a production backtest.

## Primary rules

| Rule | LONG | SHORT |
|---|---|---|
| `ema_trend` | close > EMA50, EMA20 > EMA50, 5-bar return > 0 | mirrored |
| `mean_reversion` | RSI14 < 30 and close > EMA50 | RSI14 > 70 and close < EMA50 |
| `breakout` | close > prior 20-day high | close < prior 20-day low |

## Results (10-bar spacing)

| Rule | SL×ATR | RR | Signals | Breakeven WR | Train WR | Train exp | Test WR | Test exp |
|---|---|---|---|---|---|---|---|---|
| breakout | 1.0 | 1.5 | 789 | 40.0% | 38.3% | −0.511% | 43.3% | −0.238% |
| breakout | 1.0 | 2.0 | 732 | 33.3% | 30.6% | −0.624% | 33.6% | −0.399% |
| breakout | 1.5 | 2.0 | 530 | 33.3% | 24.3% | −1.353% | 23.5% | −1.429% |
| breakout | 2.0 | 3.0 | 320 | 25.0% | 3.8% | −3.969% | 0.0% | −4.684% |
| ema_trend | 1.0 | 1.5 | 1,285 | 40.0% | 42.4% | −0.262% | 38.0% | −0.647% |
| ema_trend | 1.0 | 2.0 | 1,203 | 33.3% | 34.3% | −0.312% | 30.1% | −0.773% |
| ema_trend | 1.5 | 2.0 | 869 | 33.3% | 25.9% | −1.085% | 20.7% | −1.820% |
| ema_trend | 2.0 | 3.0 | 511 | 25.0% | 5.1% | −3.651% | 2.5% | −4.347% |
| mean_reversion | 1.0 | 1.5 | 61 | 40.0% | 37.2% | −0.561% | 22.2% | −1.181% |
| mean_reversion | 1.0 | 2.0 | 57 | 33.3% | 22.5% | −1.093% | 11.1% | −1.663% |

Without spacing the table is larger (7,705 `ema_trend` signals) but tells the same story: every configuration is negative, and overlapping labels make the sample look bigger than it is.

### The one interesting row

`ema_trend`, SL 1.0×ATR, RR 1.5: **42.4% wins against a 40% breakeven**. Gross edge ≈ +0.06R per trade — real but tiny. After 0.39% costs it is −0.26%. The edge exists; it is simply smaller than the friction.

## Meta-model on the same window

Trained on the `ema_trend` dataset (7,705 signals, 11 features, logistic regression):

| Metric | Value |
|---|---|
| Test AUC | 0.528 (permutation p = 0.055) |
| Positive rate | 30.4% |
| Expectancy, all signals | −0.692% |
| Expectancy, selected | −1.385% |
| Selected | 19 (1.6%) |
| Verdict | **NO EDGE** |

The model cannot manufacture an edge that the primary signal does not have. It correctly refused to claim one.

## Interpretation

1. **The bottleneck is the primary hypothesis, not the model.** A meta-model re-weights existing signals; it cannot flip a negative expectancy.
2. **Costs dominate short-horizon signals.** A 1.5R target with 42% wins is +0.06R gross and −0.26% net. You need either a larger gross edge or fewer, bigger trades.
3. **More features will not fix it.** Kronos, gradient boosting, or a fourth model all sit downstream of the same signal.

## Caveats

- These are **proxy rules**, not the production `signal_generator.py` ensemble. The next test should use signals the bot actually emitted, not a stand-in.
- Yahoo data, current constituents, one 1.8-year regime.
- Triple-barrier labels assume the stop is hit at the stop price; gaps through the stop are not modelled here (the backtester's slippage does).
- No position sizing, no portfolio constraints — this measures raw signal expectancy only.

## Next experiments (in order)

1. **Log real signals.** Persist every production signal with timestamp, features and outcome; evaluate that, not a proxy.
2. **Change the hypothesis, not the model.** Test index-level mean reversion, cross-sectional momentum (rank 20-day returns, long top decile), event-driven (earnings gaps), or volatility-regime filters.
3. **Raise the bar per trade.** Wider targets (RR ≥ 3) with lower win rates only work if the gross edge clears costs; the table shows it currently does not.
4. **Only then** add Kronos features and the meta-model — against a primary signal whose gross expectancy is already positive.

## Reproduce

```bash
python scripts/research_primary_rules.py \
  --panel panel_real.csv \
  --min-gap 10 \
  --out primary_rule_results.csv
```
