# Meta-Model

The meta-model answers one question: **given a primary signal, what is the probability it reaches the target before the stop?**

It does not decide direction. The primary rule does that. The meta-model decides *whether to take the trade and how much size it deserves*.

## Pipeline

```
OHLCV panel
  └─ compute_features()        ret_1/5/20, ATR%, RSI, EMA distance, volume z, range, gap, trend
  └─ primary_signal()          EMA50 trend + EMA20 alignment + 5-bar momentum
  └─ triple_barrier_labels()   target / stop / vertical, stop wins a same-bar touch
  └─ build_meta_dataset()      keep only target/stop outcomes, net return after costs
  └─ time_split()              70/15/15 chronological — never shuffled
  └─ LogisticModel             L2 logistic regression, numpy only
  └─ threshold                 chosen on validation expectancy
  └─ test metrics              AUC, Brier, calibration, precision, expectancy lift
```

## Usage

```bash
# 1. Optional: generate Kronos features first (see KRONOS_INTEGRATION.md)
python scripts/kronos_batch.py --symbols RELIANCE,TCS,INFY --out data/kronos.json

# 2. Train and evaluate
python scripts/train_meta_model.py \
  --panel data/panel.csv \
  --kronos data/kronos.json \
  --max-bars 10 --sl-atr-mult 1.0 --rr 2.0 \
  --cost-pct 0.39 \
  --out-dir artifacts/meta
```

Outputs:

| File | Contents |
|---|---|
| `artifacts/meta/meta_model.json` | feature list, weights, bias, scaler mean/std, threshold, metrics |
| `artifacts/meta/meta_metrics.json` | test metrics + verdict |

## Reading the output

| Metric | What "good" looks like |
|---|---|
| `auc` | ≥ 0.55 on the held-out period. 0.50 means the model knows nothing. |
| `auc_p_value` | Permutation test. Must be < 0.05 — a 0.56 AUC on 30 trades is noise. |
| `brier` | Lower is better; compare against the base positive rate. |
| `precision_selected` | Should be clearly above `positive_rate`. |
| `expectancy_selected_pct` | Mean net return of taken trades, after costs. Must beat `expectancy_all_pct`. |
| `lift_pct` | Difference between the two. Below ~0.05 pp is noise. |
| `selected` | Fewer than 20 selected trades = no conclusion. |
| `verdict` | `EDGE` only when all of the above hold. Otherwise `NO EDGE`. |

The calibration table tells you whether a 0.7 probability really wins ~70% of the time. If it does not, the threshold is meaningless — recalibrate (Platt/isotonic) or collect more data.

## Honest caveats

- **Threshold selection is fitted on validation.** With a few hundred rows, the chosen cut is optimistic. Re-check on a second held-out period before trusting it.
- **AUC alone is not evidence.** The verdict also requires a permutation p-value below 0.05, a positive expectancy lift, a meaningful sample, and a threshold that actually filters (5–70% selected).
- **Timeouts are dropped.** The model learns target vs stop only. A separate model (or a smaller vertical barrier) is needed if time exits matter.
- **Class balance changes with the regime.** A 62% positive rate in a bull sample is not a 62% win rate in a bear sample. Report per-year.
- **Kronos features are optional and heavily down-weighted by design.** If `kronos_available` is mostly 0, the model is a baseline model wearing a Kronos hat.
- **No leakage.** Features use only data available at the signal bar. Labels look forward — that is the point — but never join future features.

## Using the model

```python
import json, numpy as np

artifact = json.load(open("artifacts/meta/meta_model.json"))
probability = float(1 / (1 + np.exp(-(
    (np.array(features) - artifact["model"]["mean"]) / artifact["model"]["std"]
    @ artifact["model"]["weights"] + artifact["model"]["bias"]
)))
take_trade = probability >= artifact["threshold"]
```

In production, size the position as `base_risk × probability`, capped by the existing risk layer. Run it in shadow mode for 30 sessions before it touches sizing, and compare its picks against the un-filtered primary signal.
