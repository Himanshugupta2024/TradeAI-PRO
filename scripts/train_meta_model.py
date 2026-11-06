"""
Train and evaluate the meta-model.

    python scripts/train_meta_model.py \
        --panel data/panel.csv \
        --kronos data/kronos_forecasts.json \
        --out-dir artifacts/meta

The script never places orders. It writes:

    artifacts/meta/meta_model.json   weights + scaler + threshold
    artifacts/meta/meta_metrics.json test metrics + verdict
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.ai_engine.meta_labeler import (  # noqa: E402
    build_meta_dataset,
    save_result,
    train_meta_model,
)

logger = logging.getLogger("train_meta_model")


def _load_panel(args) -> pd.DataFrame:
    if args.panel:
        path = Path(args.panel)
        panel = pd.read_parquet(path) if path.suffix.lower() in (".parquet", ".pq") else pd.read_csv(path)
        if "date" in panel.columns and "timestamp" not in panel.columns:
            panel = panel.rename(columns={"date": "timestamp"})
        return panel

    from data.bhavcopy_panel import BhavcopyPanel

    start = date.fromisoformat(args.start) if args.start else date.today()
    end = date.fromisoformat(args.end) if args.end else date.today()
    symbols = [s.strip().upper() for s in (args.symbols or "").split(",") if s.strip()]
    return BhavcopyPanel().load(
        start, end, symbols=symbols or None, allow_download=not args.cache_only
    )


def _load_kronos(path: str | None) -> pd.DataFrame | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("records") or []
    if not records:
        logger.warning("Kronos file has no records; training without Kronos features")
        return None
    return pd.DataFrame(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the meta-labelling model")
    parser.add_argument("--panel", help="CSV/parquet with timestamp,symbol,OHLCV[,signal]")
    parser.add_argument("--symbols", help="comma-separated symbols (bhavcopy mode)")
    parser.add_argument("--start", help="YYYY-MM-DD (bhavcopy mode)")
    parser.add_argument("--end", help="YYYY-MM-DD (bhavcopy mode)")
    parser.add_argument("--cache-only", action="store_true")
    parser.add_argument("--kronos", help="JSON produced by scripts/kronos_batch.py")
    parser.add_argument("--max-bars", type=int, default=10)
    parser.add_argument("--sl-atr-mult", type=float, default=1.0)
    parser.add_argument("--rr", type=float, default=2.0)
    parser.add_argument("--cost-pct", type=float, default=0.39, help="round-trip cost in %% (delivery ~0.39)")
    parser.add_argument("--val-fraction", type=float, default=0.15)
    parser.add_argument("--test-fraction", type=float, default=0.15)
    parser.add_argument("--l2", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--out-dir", default="artifacts/meta")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    panel = _load_panel(args)
    if panel is None or panel.empty:
        logger.error("no panel data")
        return 3

    kronos = _load_kronos(args.kronos)

    try:
        data = build_meta_dataset(
            panel,
            kronos=kronos,
            max_bars=args.max_bars,
            sl_atr_mult=args.sl_atr_mult,
            rr_ratio=args.rr,
            cost_pct=args.cost_pct,
        )
    except ValueError as exc:
        logger.error("dataset build failed: %s", exc)
        return 5

    if len(data) < 100:
        logger.error("only %d labelled signals — need at least 100", len(data))
        return 6

    try:
        result = train_meta_model(
            data,
            val_fraction=args.val_fraction,
            test_fraction=args.test_fraction,
            l2=args.l2,
            threshold=args.threshold,
        )
    except ValueError as exc:
        logger.error("training failed: %s", exc)
        return 7

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    save_result(
        result,
        model_path=str(out_dir / "meta_model.json"),
        metrics_path=str(out_dir / "meta_metrics.json"),
    )

    metrics = result.metrics
    print("\n" + "=" * 62)
    print("  META-MODEL")
    print("=" * 62)
    print(f"  Features          : {len(result.features)}")
    print(f"  Rows train/val/test: {metrics['train_rows']}/{metrics['validation_rows']}/{metrics['test_rows']}")
    print(f"  Test positive rate : {metrics['positive_rate']:.1%}")
    print(f"  AUC               : {metrics['auc']:.4f}  (p={metrics['auc_p_value']:.3f})")
    print(f"  Brier / log loss  : {metrics['brier']:.4f} / {metrics['log_loss']:.4f}")
    print(f"  Threshold         : {metrics['threshold']:.3f}")
    print(f"  Selected          : {metrics['selected']} ({metrics['selected_share']:.1%})")
    print(f"  Precision         : {metrics['precision_selected']:.1%}")
    print(f"  Expectancy all    : {metrics['expectancy_all_pct']:+.3f}%")
    print(f"  Expectancy picked : {metrics['expectancy_selected_pct']:+.3f}%")
    print(f"  Lift              : {metrics['lift_pct']:+.3f} pp")
    print(f"  Verdict           : {result.verdict}")
    print("=" * 62)

    if result.calibration:
        print("\n  Calibration (test):")
        for row in result.calibration:
            print(f"    {row['bin']}: n={row['n']:>4}  pred={row['predicted']:.2f}  actual={row['actual']:.2f}")

    print(f"\n  Saved: {out_dir / 'meta_model.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
