# Kronos Integration

[Kronos](https://github.com/shiyu-coder/Kronos) is an MIT-licensed, decoder-only foundation model for financial K-lines, pre-trained on OHLCV from ~45 exchanges. This document explains how it fits into TradeAI PRO, and — more importantly — how it should not be used.

## What Kronos gives you

| Model | Context | Params | Open weights |
|---|---|---|---|
| Kronos-mini | 2048 | 4.1M | yes |
| Kronos-small | 512 | 24.7M | yes |
| Kronos-base | 512 | 102.3M | yes |
| Kronos-large | 512 | 499.2M | no |

Input: a lookback window of `open, high, low, close` (+ optional `volume`, `amount`) with timestamps. Output: a forecast K-line window of `pred_len` bars. Sampling is controlled by `T`, `top_p` and `sample_count`.

## Architecture: offline inference, cached features

```
GitHub Actions (or a research box)
  └─ scripts/kronos_batch.py
        ├─ bhavcopy panel (point-in-time, adjusted)
        ├─ Kronos model (mini/small) on CPU
        └─ writes data/kronos_forecasts.json  ──►  Supabase / R2
                                                        │
Render API (thin, 512 MB)  ◄───────────────────────────┘
  └─ serves cached kronos_* features with an as-of timestamp
        └─ dashboard / signal ensemble
```

**Do not** import torch or Kronos inside the API process. A CPU torch install plus weights will not fit comfortably in 512 MB, and a cold start would take minutes.

## Feature contract

`agents/ai_engine/kronos_forecaster.py` converts forecast paths into:

| Feature | Meaning |
|---|---|
| `kronos_expected_return_pct` | Mean terminal return across sampled paths |
| `kronos_prob_up` | Share of paths finishing above the last close |
| `kronos_q05_return_pct` | 5th percentile terminal return (downside) |
| `kronos_q95_return_pct` | 95th percentile terminal return (upside) |
| `kronos_dispersion_pct` | Std-dev of terminal returns (model uncertainty) |
| `kronos_path_vol_pct` | Mean per-bar volatility inside the forecast path |

These are **features**, not orders. They should enter the existing ensemble/quality scorer, be calibrated, and be evaluated like any other feature.

> With `n_paths=1` there is no distribution: `prob_up` collapses to 0/1 and dispersion is 0. Use at least 8 paths for anything probability-like.

## Running it

```bash
pip install -r requirements-kronos.txt
git clone https://github.com/shiyu-coder/Kronos third_party/Kronos

# 20 NIFTY names, 8 sampled paths, mini model, CPU
python scripts/kronos_batch.py \
  --symbols RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK \
  --start 2024-01-01 --end 2024-12-31 \
  --model mini --n-paths 8 --pred-len 5 \
  --out data/kronos_forecasts.json
```

If the repo or torch is missing the script exits with code `2` and a clear message; nothing else in the app breaks.

## Evaluation protocol (non-negotiable)

1. **Walk-forward only.** Fit/validate on rolling windows; never evaluate on data the model saw in pre-training. Kronos was trained through 2024 on 45 exchanges — assume NSE daily bars up to that point are contaminated. Prefer 2025+ out-of-sample data.
2. **Same labels, same costs.** Use the triple-barrier labels and the delivery cost profile from `agents/backtester`. A forecast is only useful if it survives ~39 bps round trip.
3. **Baselines first.** Compare against 12-1 momentum, 20/50 EMA trend and RSI(2). If Kronos features do not beat them out-of-sample, do not ship them.
4. **Portfolio, not symbol.** Feed signals into `PortfolioBacktester`; a per-symbol IC of 0.03 can disappear after position limits.
5. **Shadow mode.** Publish `kronos_*` alongside the current signal for 30 sessions before using them in sizing.

## Risks and limits

| Risk | Detail |
|---|---|
| **Pre-training contamination** | The model has seen a lot of market history. Any "backtest" before its training cutoff is not out-of-sample. |
| **Domain shift** | Training is global and crypto-heavy; Indian cash equities may behave differently. Fine-tuning needs a GPU and a careful split. |
| **Context length** | 512 bars for small/base; 2048 for mini. Longer history is truncated, not summarised. |
| **Sampling variance** | Forecasts are stochastic. Cache the seed/settings with the output, and use enough paths. |
| **Latency** | CPU inference for 5 bars × 8 paths is seconds per symbol, not milliseconds. Batch it. |
| **Model drift** | Pin the exact HF revision (`model_id@revision`) in production; a silent upstream update changes your features. |
| **False precision** | A forecast path looks like a price prediction. It is a distribution over noisy data — treat the quantiles, not the mean, as the signal. |

## What not to do

- Do not place orders from `kronos_expected_return_pct` directly.
- Do not run Kronos inside the Render web service.
- Do not fine-tune on fewer than a few thousand windows and call it validated.
- Do not report a backtest that starts before the model's training cutoff.
- Do not compare a 5-bar forecast to a 5-day forward return without matching the bar interval.

## Suggested first experiment

5 symbols × 2 years of daily bars, `mini`, 8 paths, 5-bar horizon:

1. Compute features only on dates where the existing signal is actionable.
2. Split: 2025-01-01 → 2025-06-30 for fitting the meta-model, 2025-07-01 → today for evaluation.
3. Target: triple-barrier `tb_label` (1/0) for the meta-model.
4. Metric: lift in expectancy (R) over the meta-model without Kronos features, after costs.

If the lift is not positive and stable across both halves, stop there.
