# PIT Universe + Beta-Neutral Reversal

The earlier reversal result had two holes: beta ≈ 1 and a survivor-only universe. This experiment closes the beta hole and builds the machinery for the survivorship hole.

## Method

| Item | Value |
|---|---|
| Universe | 39 current NIFTY large caps (Yahoo) |
| Window | 2025-01-01 → 2026-11-05 (458 sessions) |
| Signal | 20-day return, weakest quintile, long only |
| Rebalance | every 10 / 15 / 21 bars |
| Hedge | none / equal (100% short universe) / rolling beta (60-day, clipped 0–1.5) |
| Costs | 0.39% round trip × turnover, both legs |
| Membership | optional `symbol,start,end` CSV |

`--membership` filters the ranking each day: a symbol outside its window cannot enter the basket. Without that file, survivorship is **not** controlled, and the script says so.

## Results — all names

| Hold | Hedge | CAGR | Vol | Sharpe | Beta | Alpha (ann.) |
|---|---|---|---|---|---|---|
10 | none | 12.90% | 16.84% | 0.81 | 1.011 | 6.20% |
10 | equal | 5.09% | 9.73% | 0.57 | 0.075 | 4.95% |
10 | beta | 5.57% | 10.54% | 0.57 | 0.149 | 4.95% |
15 | none | 13.51% | 16.60% | 0.86 | 0.968 | 7.02% |
15 | equal | 5.58% | 10.09% | 0.60 | 0.032 | 5.77% |
15 | beta | 5.59% | 10.96% | 0.56 | 0.101 | 5.36% |
21 | none | 11.15% | 15.27% | 0.78 | 0.884 | 5.30% |
21 | equal | 3.23% | 9.40% | 0.39 | −0.052 | 4.05% |
21 | beta | 4.30% | 10.14% | 0.47 | 0.119 | 3.89% |

Benchmark: **6.69% CAGR, 0.56 Sharpe**.

## Stress test — remove the five best performers

DIVISLAB, HINDALCO, EICHERMOT, ADANIPORTS, BAJFINANCE removed:

| Hedge | CAGR | Sharpe | Beta | Alpha (ann.) |
|---|---|---|---|---|
none | 11.29% | 0.73 | 0.975 | 8.12% |
equal | 6.64% | 0.65 | 0.034 | 6.97% |
beta | 5.48% | 0.52 | 0.120 | 5.57% |

Alpha survives (5.6% vs 5.4% before), so it is not concentrated in a handful of names.

## Interpretation

1. **The beta critique holds.** Unhedged, ~95% of the story is market exposure. Hedged, beta falls to ~0.1 and alpha is 4–5.5% annualised.
2. **But risk-adjusted, it is not better than the benchmark.** Hedged Sharpe 0.47–0.60 vs benchmark 0.56. The alpha is real but small relative to the volatility you carry.
3. **Hedging costs are understated.** Shorting 39 names is not practical; NIFTY futures would be used, and the basis/roll and margin costs are not in this table. Add 1–2% annualised of drag and the edge mostly disappears.
4. **Survivorship is still open.** No membership file was available, so the basket was chosen from today's index members. The earlier caveat stands.

## Verdict

> Alpha exists on paper (~5% annualised, beta-neutral) but is not large enough to be compelling after realistic hedging costs, and it has not been tested on a survivorship-free universe.

Do not trade it yet. The next step that would change the verdict is a true point-in-time universe, not another parameter sweep.

## Usage

```bash
# Reversal, beta-hedged, with a membership file
python scripts/research_pit_beta_neutral.py \
  --panel panel_real.csv \
  --membership membership.csv \
  --lookback 20 --holds 10,15,21 --quantile 0.2 \
  --hedge beta --out pit_beta.csv
```

Membership schema:

```csv
symbol,start,end
RELIANCE,2019-01-01,
YESBANK,2019-01-01,2020-03-31
```

`end` empty means still a member. The script drops rows outside the window before ranking.
