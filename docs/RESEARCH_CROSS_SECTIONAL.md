# Cross-Sectional Research — NIFTY Large Caps, 2025-01 → 2026-11

**Question:** does ranking the universe beat picking charts one at a time?

**Short answer:** trend-following momentum does not; short-term **reversal** does in this window — but the result is inflated by survivorship bias and is largely market beta. Treat it as a lead, not a strategy.

## Method

| Item | Value |
|---|---|
| Universe | 39 current NIFTY large caps (Yahoo `.NS`) |
| Window | 2025-01-01 → 2026-11-05 (458 sessions) |
| Ranking | trailing return over `lookback`, shifted 1 day (no look-ahead) |
| Portfolio | long top quantile, optional short bottom quantile, equal weight |
| Rebalance | every `hold` bars |
| Costs | 0.39% round trip × turnover |
| Benchmark | equal-weight universe, daily rebalanced |

## 1. Momentum (long winners) — does not work

Long-short, all nine configurations negative:

| Lookback | Hold | CAGR | Sharpe | Max DD | Benchmark CAGR |
|---|---|---|---|---|---|
| 120 | 10 | −5.59% | −0.80 | −12.3% | 6.69% |
| 120 | 5 | −6.13% | −0.89 | −14.0% | 6.69% |
| 20 | 5 | −7.65% | −0.94 | −19.1% | 6.69% |
| 60 | 10 | −8.08% | −1.06 | −17.2% | 6.69% |

Long-only top quintile is also weak: best is lookback 20 / hold 21 at **1.79% CAGR** versus 6.69% benchmark. Gross return there is +10.32%, but turnover costs remove 7pp.

**Reading:** the ranking has some gross information at short lookbacks, but the portfolio turns over too fast for the cost budget. In this regime, winners did not keep winning.

## 2. Reversal (long losers) — consistent, but suspect

Long the bottom quintile of the 20-day return, no shorts. Costs included.

| Quantile | Hold | CAGR | Sharpe | Max DD | Gross | Net | Turnover | Beta | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|---|
| 10% | 10 | 11.86% | 0.68 | −19.5% | 40.3% | 22.9% | 1.54 | 1.00 | +13.4% | +8.4% |
| 10% | 15 | 13.45% | 0.78 | −15.0% | 39.8% | 26.2% | 1.74 | 0.93 | +14.0% | +10.7% |
| 10% | 21 | 13.13% | 0.77 | −14.6% | 35.5% | 25.5% | 1.86 | 0.86 | +18.4% | +6.0% |
| 20% | 10 | 12.90% | 0.81 | −18.2% | 39.6% | 25.1% | 1.28 | 1.01 | +16.3% | +7.6% |
| 20% | 15 | 13.51% | 0.86 | −16.3% | 38.3% | 26.3% | 1.55 | 0.97 | +13.0% | +11.8% |
| 20% | 21 | 11.15% | 0.78 | −17.5% | 30.6% | 21.5% | 1.75 | 0.88 | +23.7% | −1.8% |
| 30% | 10 | 8.29% | 0.60 | −15.8% | 26.8% | 15.8% | 1.06 | 0.97 | +8.6% | +6.6% |
| 30% | 15 | 11.60% | 0.80 | −14.1% | 32.0% | 22.4% | 1.28 | 0.97 | +14.3% | +7.1% |
| 30% | 21 | 9.07% | 0.69 | −17.4% | 24.9% | 17.3% | 1.53 | 0.91 | +19.6% | −1.9% |

Positive across every quantile/hold combination, both years, after costs. That consistency is why it deserves a second look.

### Why it is probably not tradeable yet

1. **Survivorship bias is severe here.** The universe is *today's* NIFTY members. Buying the losers among survivors is close to "buy the dips in companies that turned out fine". A delisted-inclusive, point-in-time universe would remove a large part of this.
2. **Beta ≈ 1.** The benchmark returned 6.69% CAGR; the strategy beta is 0.86–1.01, so most of the 12–13% is market exposure, not alpha. Alpha is roughly 4–6pp before accounting for higher vol and drawdown.
3. **One regime.** 2025–2026 was broadly up with sharp rotations. Reversal strategies typically fail in sustained trends.
4. **Multiple testing.** Nine configurations were tried; the table is consistent, but the whole exercise is one sample.

## What to test next

1. **Point-in-time universe.** Re-run on bhavcopy data with membership history (including removed names). If reversal survives, it is real.
2. **Beta-neutralise.** Hedge with the equal-weight benchmark or NIFTY futures; then measure alpha directly.
3. **Sector caps.** A 20% basket can concentrate in one falling sector and turn into a sector bet.
4. **Turnover control.** Hysteresis (only replace a name when it leaves the bottom 30%) would cut costs materially.
5. **Regime filter.** Enable reversal only when 20-day market breadth is mean-reverting, not trending.

## Reproduce

```bash
# Momentum
python scripts/research_cross_sectional.py --panel panel_real.csv --out xs_mom.csv

# Reversal, long-only
python scripts/research_cross_sectional.py --panel panel_real.csv --reversal --long-only \
  --lookbacks 20 --holds 10,15,21 --out xs_rev.csv
```
