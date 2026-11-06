"""
Batch Kronos inference for TradeAI PRO.

Run this offline (GitHub Actions or a research machine), never inside the
512 MB Render web process. It writes a JSON file of forecast features that the
API / dashboard can read.

Examples:

    # From the local bhavcopy cache
    python scripts/kronos_batch.py --symbols RELIANCE,TCS,INFY --out data/kronos.json

    # From a prepared panel CSV (date,symbol,open,high,low,close,volume)
    python scripts/kronos_batch.py --panel data/panel.csv --limit 50 --out data/kronos.json

    # Bigger model, more paths, GPU
    python scripts/kronos_batch.py --model small --n-paths 16 --device cuda:0 --out data/kronos.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

# Allow `python scripts/kronos_batch.py` from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ai_engine.kronos_forecaster import (  # noqa: E402
    MODEL_CATALOG,
    KronosConfig,
    KronosForecaster,
    KronosUnavailable,
)

logger = logging.getLogger("kronos_batch")


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def _load_panel(args) -> pd.DataFrame:
    if args.panel:
        path = Path(args.panel)
        if path.suffix.lower() in (".parquet", ".pq"):
            panel = pd.read_parquet(path)
        else:
            panel = pd.read_csv(path)
        if "date" in panel.columns:
            panel["date"] = pd.to_datetime(panel["date"], errors="coerce")
        return panel

    # Fall back to the official bhavcopy panel (cache first)
    from data.bhavcopy_panel import BhavcopyPanel

    start = date.fromisoformat(args.start) if args.start else date.today()
    end = date.fromisoformat(args.end) if args.end else date.today()
    return BhavcopyPanel().load(
        start,
        end,
        symbols=_parse_symbols(args.symbols) or None,
        allow_download=not args.cache_only,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Batch Kronos forecast features")
    parser.add_argument("--symbols", help="comma-separated symbols")
    parser.add_argument("--panel", help="CSV/parquet panel with date,symbol,OHLCV")
    parser.add_argument("--start", help="YYYY-MM-DD (bhavcopy mode)")
    parser.add_argument("--end", help="YYYY-MM-DD (bhavcopy mode)")
    parser.add_argument("--model", default="mini", choices=sorted(MODEL_CATALOG))
    parser.add_argument("--device", default=os.getenv("KRONOS_DEVICE", "cpu"))
    parser.add_argument("--pred-len", type=int, default=5)
    parser.add_argument("--n-paths", type=int, default=8)
    parser.add_argument("--lookback", type=int, default=400)
    parser.add_argument("--limit", type=int, default=0, help="max symbols (0 = all)")
    parser.add_argument("--cache-only", action="store_true", help="do not download bhavcopy")
    parser.add_argument("--out", default="data/kronos_forecasts.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not KronosForecaster.is_available():
        logger.error(
            "Kronos not available. Install requirements-kronos.txt and clone "
            "https://github.com/shiyu-coder/Kronos into third_party/Kronos "
            "(or set KRONOS_REPO_PATH)."
        )
        return 2

    panel = _load_panel(args)
    if panel is None or panel.empty:
        logger.error("no market data found for the requested range")
        return 3

    if "date" in panel.columns:
        panel = panel.rename(columns={"date": "timestamp"})

    symbols = _parse_symbols(args.symbols)
    if not symbols:
        symbols = sorted(panel["symbol"].astype(str).str.upper().unique().tolist())
    if args.limit:
        symbols = symbols[: args.limit]

    config = KronosConfig.for_model(
        args.model,
        device=args.device,
        pred_len=args.pred_len,
        lookback=args.lookback,
    )
    forecaster = KronosForecaster(config=config)

    records = []
    failures = 0
    for symbol in symbols:
        frame = panel[panel["symbol"].astype(str).str.upper() == symbol]
        if frame.empty or len(frame) < 30:
            logger.warning("%s: insufficient history (%d bars)", symbol, len(frame))
            failures += 1
            continue
        try:
            forecast = forecaster.forecast(
                frame, symbol=symbol, pred_len=args.pred_len, n_paths=args.n_paths
            )
            records.append(forecast.as_record())
            logger.info(
                "%s: expected %.2f%% | p(up)=%.2f | q05 %.2f%% | q95 %.2f%%",
                symbol,
                forecast.features["kronos_expected_return_pct"],
                forecast.features["kronos_prob_up"],
                forecast.features["kronos_q05_return_pct"],
                forecast.features["kronos_q95_return_pct"],
            )
        except Exception as exc:
            failures += 1
            logger.error("%s: forecast failed: %s", symbol, exc)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": config.model_id,
        "pred_len": args.pred_len,
        "n_paths": args.n_paths,
        "records": records,
        "failures": failures,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("wrote %d records to %s (%d failures)", len(records), out, failures)

    return 0 if records else 4


if __name__ == "__main__":
    raise SystemExit(main())
