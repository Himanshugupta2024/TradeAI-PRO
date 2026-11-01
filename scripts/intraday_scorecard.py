from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

DEFAULT_CSV = Path("data/intraday_scorecard.csv")

HEADERS = [
    "date",
    "symbol",
    "setup",
    "direction",
    "entry",
    "stop",
    "target1",
    "target2",
    "risk_pct",
    "rr",
    "trend_score",
    "confluence_score",
    "volume_score",
    "risk_score",
    "news_score",
    "total_score",
    "grade",
    "outcome_r",
    "win",
    "rule_break",
    "missed_a",
    "notes",
]


@dataclass
class TradeRow:
    date: str
    symbol: str
    setup: str
    direction: str
    entry: float
    stop: float
    target1: float
    target2: float
    risk_pct: float
    rr: float
    trend_score: int
    confluence_score: int
    volume_score: int
    risk_score: int
    news_score: int
    total_score: int
    grade: str
    outcome_r: float
    win: int
    rule_break: int
    missed_a: int
    notes: str

    def to_dict(self) -> dict[str, str]:
        return {
            "date": self.date,
            "symbol": self.symbol,
            "setup": self.setup,
            "direction": self.direction,
            "entry": f"{self.entry:.4f}",
            "stop": f"{self.stop:.4f}",
            "target1": f"{self.target1:.4f}",
            "target2": f"{self.target2:.4f}",
            "risk_pct": f"{self.risk_pct:.4f}",
            "rr": f"{self.rr:.4f}",
            "trend_score": str(self.trend_score),
            "confluence_score": str(self.confluence_score),
            "volume_score": str(self.volume_score),
            "risk_score": str(self.risk_score),
            "news_score": str(self.news_score),
            "total_score": str(self.total_score),
            "grade": self.grade,
            "outcome_r": f"{self.outcome_r:.4f}",
            "win": str(self.win),
            "rule_break": str(self.rule_break),
            "missed_a": str(self.missed_a),
            "notes": self.notes,
        }


def _safe_float(value: str) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _grade(total_score: int) -> str:
    if total_score >= 8:
        return "A"
    if total_score >= 6:
        return "B"
    return "C"


def _calc_rr(entry: float, stop: float, target1: float) -> float:
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0
    reward = abs(target1 - entry)
    return reward / risk


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def init_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"CSV already exists: {path}")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
    print(f"Created scorecard CSV: {path}")


def add_trade(path: Path, args: argparse.Namespace) -> None:
    if not path.exists():
        init_csv(path)

    total_score = (
        int(args.trend_score)
        + int(args.confluence_score)
        + int(args.volume_score)
        + int(args.risk_score)
        + int(args.news_score)
    )
    grade = _grade(total_score)

    rr = float(args.rr) if args.rr is not None else _calc_rr(args.entry, args.stop, args.target1)
    win = 1 if float(args.outcome_r) > 0 else 0

    row = TradeRow(
        date=args.date,
        symbol=args.symbol.upper().strip(),
        setup=args.setup,
        direction=args.direction.upper().strip(),
        entry=float(args.entry),
        stop=float(args.stop),
        target1=float(args.target1),
        target2=float(args.target2),
        risk_pct=float(args.risk_pct),
        rr=float(rr),
        trend_score=int(args.trend_score),
        confluence_score=int(args.confluence_score),
        volume_score=int(args.volume_score),
        risk_score=int(args.risk_score),
        news_score=int(args.news_score),
        total_score=int(total_score),
        grade=grade,
        outcome_r=float(args.outcome_r),
        win=win,
        rule_break=int(args.rule_break),
        missed_a=int(args.missed_a),
        notes=args.notes or "",
    )

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writerow(row.to_dict())

    print(
        "Added trade:",
        f"symbol={row.symbol}",
        f"grade={row.grade}",
        f"score={row.total_score}/10",
        f"rr={row.rr:.2f}",
    )


def _iter_rows(path: Path) -> Iterable[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            yield row


def weekly_summary(path: Path, days: int) -> None:
    if not path.exists():
        print(f"CSV not found: {path}")
        return

    start_dt = datetime.now() - timedelta(days=max(1, int(days)))
    rows = []
    for row in _iter_rows(path):
        try:
            d = _parse_date(str(row.get("date", "")))
        except Exception:
            continue
        if d >= start_dt:
            rows.append(row)

    if not rows:
        print(f"No rows found in last {days} days")
        return

    trades = len(rows)
    wins = sum(_safe_int(r.get("win", "0")) for r in rows)
    win_rate = (wins / trades) * 100.0

    outcome_rs = [_safe_float(r.get("outcome_r", "0")) for r in rows]
    avg_r = sum(outcome_rs) / max(1, len(outcome_rs))

    # R-unit equity curve drawdown
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in outcome_rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    rule_breaks = sum(_safe_int(r.get("rule_break", "0")) for r in rows)
    a_trades = sum(1 for r in rows if str(r.get("grade", "")).upper() == "A")
    missed_a = sum(_safe_int(r.get("missed_a", "0")) for r in rows)

    print(f"Weekly summary ({days}d)")
    print(f"trades={trades}")
    print(f"win_rate={win_rate:.2f}%")
    print(f"avg_r={avg_r:.2f}")
    print(f"max_drawdown_r={max_dd:.2f}")
    print(f"rule_break_count={rule_breaks}")
    print(f"a_grade_trades={a_trades}")
    print(f"missed_a_trades={missed_a}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Intraday scorecard logger and weekly stats helper"
    )
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to scorecard CSV")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Create scorecard CSV with headers")
    p_init.set_defaults(func=lambda p, a: init_csv(p))

    p_add = sub.add_parser("add", help="Add one scored trade row")
    p_add.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p_add.add_argument("--symbol", required=True)
    p_add.add_argument("--setup", required=True)
    p_add.add_argument("--direction", required=True, choices=["LONG", "SHORT"])
    p_add.add_argument("--entry", required=True, type=float)
    p_add.add_argument("--stop", required=True, type=float)
    p_add.add_argument("--target1", required=True, type=float)
    p_add.add_argument("--target2", required=True, type=float)
    p_add.add_argument("--risk-pct", dest="risk_pct", default=1.0, type=float)
    p_add.add_argument("--rr", type=float, default=None)
    p_add.add_argument("--trend-score", required=True, type=int, choices=range(0, 3))
    p_add.add_argument("--confluence-score", required=True, type=int, choices=range(0, 4))
    p_add.add_argument("--volume-score", required=True, type=int, choices=range(0, 3))
    p_add.add_argument("--risk-score", required=True, type=int, choices=range(0, 3))
    p_add.add_argument("--news-score", required=True, type=int, choices=range(0, 2))
    p_add.add_argument("--outcome-r", default=0.0, type=float)
    p_add.add_argument("--rule-break", default=0, type=int, choices=[0, 1])
    p_add.add_argument("--missed-a", default=0, type=int, choices=[0, 1])
    p_add.add_argument("--notes", default="")
    p_add.set_defaults(func=add_trade)

    p_week = sub.add_parser("weekly", help="Compute weekly scorecard stats")
    p_week.add_argument("--days", default=7, type=int)
    p_week.set_defaults(func=lambda p, a: weekly_summary(p, int(a.days)))

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    csv_path = Path(args.csv)
    args.func(csv_path, args)


if __name__ == "__main__":
    main()
