"""
Offline champion/challenger evaluation for Kronos.

Joins a batch forecast payload (``scripts/kronos_batch.py`` output) with realised
forward returns from an OHLC panel, builds paired observations, and runs the
promotion gate from ``agents/ai_engine/champion_challenger.py``.

The incumbent used here is deliberately boring: 12-1 cross-sectional momentum,
one of the baselines named in ``docs/KRONOS_INTEGRATION.md``. If the challenger
cannot beat a momentum sign over the same dates, it has no business touching
sizing. Swap in the live signal later by writing paired observations directly
through the ledger API.

Probabilities are walk-forward empirical hit rates (Laplace-smoothed, prior
observations only). They are crude but honest: no future outcome leaks into the
score for an earlier row, and no probability is invented before there is
evidence to support it.

Examples:

    python scripts/kronos_promotion.py \
        --forecasts data/kronos_forecasts.json \
        --panel data/panel.csv

    # Persist the resulting ledger to runtime_state (needs DATABASE_URL)
    python scripts/kronos_promotion.py --forecasts ... --panel ... --persist

Exit codes:
    0  gate open — promotion is warranted
    1  gate closed — champion stays
    2  input problem (missing files, unusable panel, no resolved pairs)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ai_engine.champion_challenger import (  # noqa: E402
    ChampionChallengerLedger,
    PromotionPolicy,
)

logger = logging.getLogger("kronos_promotion")

MOMENTUM_LOOKBACK = 252
MOMENTUM_SKIP = 21
MIN_PRIOR_FOR_PROB = 20


def _sign(value: float) -> float:
    if value > 0:
        return 1.0
    if value < 0:
        return -1.0
    return 0.0


def _load_panel(path: str) -> pd.DataFrame:
    frame = pd.read_parquet(path) if Path(path).suffix.lower() in (".parquet", ".pq") else pd.read_csv(path)
    date_col = "date" if "date" in frame.columns else "timestamp" if "timestamp" in frame.columns else None
    if date_col is None or "symbol" not in frame.columns or "close" not in frame.columns:
        raise ValueError("panel needs symbol, close and a date/timestamp column")
    frame = frame.rename(columns={date_col: "date"})
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["symbol"] = frame["symbol"].astype(str).str.upper()
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.dropna(subset=["date", "close"]).sort_values(["symbol", "date"])


def _load_forecast_records(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("records") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("forecast payload has no 'records' list")
    return records


def build_pairs(records: list[dict[str, Any]], panel: pd.DataFrame) -> list[dict[str, Any]]:
    """Turn forecast records into paired momentum-vs-Kronos observations."""
    pairs: list[dict[str, Any]] = []
    grouped = {symbol: group.reset_index(drop=True) for symbol, group in panel.groupby("symbol")}

    for record in records:
        symbol = str(record.get("symbol") or "").upper()
        expected = record.get("kronos_expected_return_pct")
        if not symbol or expected is None:
            continue
        frame = grouped.get(symbol)
        if frame is None or len(frame) < MOMENTUM_LOOKBACK + 1:
            continue

        as_of = pd.to_datetime(record.get("as_of"), errors="coerce")
        if pd.isna(as_of):
            continue
        hits = frame.index[frame["date"] == as_of]
        if len(hits) == 0:
            continue
        pos = int(hits[0])
        if pos < MOMENTUM_LOOKBACK:
            continue

        closes = frame["close"].to_numpy(dtype=float)
        anchor = pos - MOMENTUM_SKIP
        base = pos - MOMENTUM_LOOKBACK
        if base < 0 or anchor <= base or closes[base] <= 0:
            continue
        momentum = closes[anchor] / closes[base] - 1.0

        horizon = int(record.get("pred_len") or 5)
        forward = pos + max(horizon, 1)
        forward_return = None
        if forward < len(closes) and closes[pos] > 0:
            forward_return = (closes[forward] / closes[pos] - 1.0) * 100.0

        pairs.append(
            {
                "symbol": symbol,
                "as_of": as_of.date().isoformat(),
                "horizon": horizon,
                "champion_score": _sign(momentum),
                "challenger_score": _sign(float(expected)),
                "forward_return_pct": forward_return,
            }
        )

    pairs.sort(key=lambda row: (row["as_of"], row["symbol"]))
    return pairs


def attach_walk_forward_probabilities(pairs: list[dict[str, Any]]) -> None:
    """Fill prob_up from prior realised hits only (no lookahead)."""
    for side, score_key, prob_key in (
        ("champion", "champion_score", "champion_prob_up"),
        ("challenger", "challenger_score", "challenger_prob_up"),
    ):
        hits = 0
        seen = 0
        for row in pairs:
            ret = row.get("forward_return_pct")
            score = row.get(score_key) or 0.0
            if seen >= MIN_PRIOR_FOR_PROB and score != 0.0:
                row[prob_key] = round(min(0.95, max(0.05, (hits + 1) / (seen + 2))), 4)
            if ret is not None and score != 0.0:
                seen += 1
                if score * ret > 0:
                    hits += 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Kronos champion/challenger evaluation")
    parser.add_argument("--forecasts", required=True, help="kronos_batch.py JSON output")
    parser.add_argument("--panel", required=True, help="CSV/parquet panel with symbol,date,close")
    parser.add_argument("--persist", action="store_true", help="save the ledger to runtime_state")
    parser.add_argument("--state-key", default=None, help="override the runtime_state key")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        records = _load_forecast_records(args.forecasts)
        panel = _load_panel(args.panel)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.error("input error: %s", exc)
        return 2

    pairs = build_pairs(records, panel)
    resolved = [row for row in pairs if row.get("forward_return_pct") is not None]
    if not resolved:
        logger.error("no resolved pairs (need forward bars after each as_of)")
        return 2

    attach_walk_forward_probabilities(pairs)

    ledger = ChampionChallengerLedger(
        policy=PromotionPolicy.from_settings(),
        state_key=args.state_key,
    )
    for row in pairs:
        ledger.record(
            symbol=row["symbol"],
            as_of=row["as_of"],
            champion_score=row["champion_score"],
            challenger_score=row["challenger_score"],
            forward_return_pct=row.get("forward_return_pct"),
            champion_prob_up=row.get("champion_prob_up"),
            challenger_prob_up=row.get("challenger_prob_up"),
            horizon=row["horizon"],
        )

    decision = ledger.evaluate()
    status = ledger.status()

    if args.persist:
        status["persisted"] = ledger.save()

    print(json.dumps({"decision": decision.as_dict(), "status": status}, indent=2, default=str))
    return 0 if decision.action == "promote" else 1


if __name__ == "__main__":
    raise SystemExit(main())
