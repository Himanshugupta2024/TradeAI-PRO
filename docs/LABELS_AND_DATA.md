# Labels and Point-in-Time Data

Two upgrades to how training data is built:

1. **Triple-barrier labels** — the label describes what a trade would actually have done, not what the next candle did.
2. **Point-in-time bhavcopy panel** — official NSE EOD data, split/bonus adjusted, restricted to the symbols that were actually in the universe on each date.

---

## 1. Triple-barrier labels

`agents/feature_engine/label_generator.py`

| Barrier | Level | Outcome |
|---|---|---|
| Upper (target) | `entry + rr_ratio × sl_distance` | `tb_label = 1` |
| Lower (stop) | `entry − sl_distance` | `tb_label = -1` |
| Vertical (time) | `max_bars` after entry | `tb_label = 0` |

Rules:

- `sl_distance = ATR_at_entry × sl_atr_mult` — volatility scaled, no future data.
- A bar that touches **both** barriers resolves to the **stop** (conservative).
- Works for LONG and SHORT. `side="auto"` reads the `signal` column.
- Bars with no signal are left as `NaN`, not silently labelled 0.

```python
from agents.feature_engine import triple_barrier_labels, triple_barrier_stats

tb = triple_barrier_labels(
    df,
    side="auto",
    max_bars=10,
    sl_atr_mult=1.0,
    rr_ratio=2.0,
    atr_col="ATR_14",
)

print(triple_barrier_stats(tb))
```

Output columns: `tb_label`, `tb_side`, `tb_barrier` (`target`/`stop`/`vertical`), `tb_bars_held`, `tb_exit_price`, `tb_return_pct`.

### Why not the old 1R/2R helper?

`label_1r2r_outcome` remains for backward compatibility, but it resolves a same-bar TP/SL touch in favour of the **target** — optimistic. The triple-barrier version is conservative and records *when* the barrier was hit, which matters for time-stop analysis and for matching the backtester's execution model.

### Meta-labelling

The primary model decides direction. A second model should decide **whether to take the trade and how big to size it**.

```python
from agents.feature_engine import meta_labels_from_triple_barrier

meta = meta_labels_from_triple_barrier(tb, df["signal"])
# 1 = target reached, 0 = stopped, NaN = no signal
```

Train the meta-model on features available at signal time; it predicts success, not direction. This is usually worth more than adding a fourth model to the ensemble.

---

## 2. Point-in-time bhavcopy panel

`data/bhavcopy_panel.py`

```python
from datetime import date
from data.bhavcopy_panel import BhavcopyPanel, PointInTimeUniverse

panel = BhavcopyPanel().load(
    date(2019, 1, 1),
    date(2024, 12, 31),
    symbols=None,          # optional pre-filter
    allow_download=True,   # False = cache only (tests/offline)
    actions=actions_df,    # optional split/bonus adjustment
)

closes = BhavcopyPanel.to_wide(panel, "close")
```

- Uses the official UDiFF bhavcopy (`data/providers/nse_bhavcopy.py`) — free, keyless, authoritative.
- `fetch_daily_exact()` never backtracks a holiday to an older file. A holiday is simply absent, so dates stay truthful.
- Columns: `date, symbol, open, high, low, close, prev_close, volume, turnover`.
- Cached per day as gzip under `.cache/bhavcopy/`.

### Corporate actions

```python
actions = pd.DataFrame([
    {"symbol": "ABC", "ex_date": "2024-06-14", "ratio": 2.0},  # 1:2 split or 1:1 bonus
])
panel = BhavcopyPanel().load(start, end, actions=actions)
```

`ratio` = new shares per old share. Prices before `ex_date` are divided by the ratio, volume multiplied. Without this, a 1:2 split looks like a −50% day and stops out every historical trade.

### Survivorship

```python
universe = PointInTimeUniverse.from_records([
    {"symbol": "ABC", "start": "2019-01-01", "end": "2022-03-31"},
    {"symbol": "XYZ", "start": "2022-04-01", "end": None},
])

panel = universe.filter(panel)
```

This removes rows for symbols outside their index-membership window. It only works if you supply real membership history — the code cannot invent it. `PointInTimeUniverse.static()` exists for an explicit, acknowledged shortcut.

---

## 3. What this does not fix yet

| Gap | Effect | Next step |
|---|---|---|
| No historical index membership source | `PointInTimeUniverse` is only as good as the table you feed it | Import NSE index constituent change history, or build a delisted-symbol list |
| Dividends not adjusted | Total-return series is understated | Add dividend events to the action table with `ratio = 1 - div/price` or a separate adjustment |
| NSE archive availability | Old dates may 404 or rate-limit | Retry with backoff, and cache aggressively; do not silently substitute another date |
| Label horizon is bar-count based | Different timeframes mean different calendar horizons | Pass `max_bars` per timeframe, or add a calendar-day vertical barrier |

---

## 4. Tests

```bash
pytest -m "not integration" -q
```

- `tests/test_triple_barrier.py` — target/stop/vertical, same-bar conservative rule, SHORT, NaN handling, stats, meta-labels
- `tests/test_bhavcopy_panel.py` — panel build, symbol filter, split adjustment, no-op actions, PIT universe filtering, date validation
