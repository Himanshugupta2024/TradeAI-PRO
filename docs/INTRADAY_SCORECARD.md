# Intraday Scorecard CLI

This helper gives you a practical, daily scorecard workflow:
- Keep a structured intraday trade log in CSV
- Auto-calculate total score and grade (A/B/C)
- Generate weekly performance stats in R units

## Files

- `scripts/intraday_scorecard.py` - CLI utility
- `data/intraday_scorecard_template.csv` - blank template with required headers

## Quick Start

Initialize CSV:

```powershell
python scripts/intraday_scorecard.py init
```

Add one trade row:

```powershell
python scripts/intraday_scorecard.py add \
  --symbol RELIANCE \
  --setup ORB \
  --direction LONG \
  --entry 2950 \
  --stop 2928 \
  --target1 2992 \
  --target2 3015 \
  --risk-pct 1.0 \
  --trend-score 2 \
  --confluence-score 3 \
  --volume-score 2 \
  --risk-score 2 \
  --news-score 1 \
  --outcome-r 1.2 \
  --rule-break 0 \
  --missed-a 0 \
  --notes "Clean breakout with volume"
```

Generate weekly summary:

```powershell
python scripts/intraday_scorecard.py weekly --days 7
```

## Scoring Rules

Total score is out of 10:
- trend_score: 0-2
- confluence_score: 0-3
- volume_score: 0-2
- risk_score: 0-2
- news_score: 0-1

Grade mapping:
- A: 8-10
- B: 6-7
- C: 0-5

## Weekly Output Metrics

- trades
- win_rate
- avg_r
- max_drawdown_r
- rule_break_count
- a_grade_trades
- missed_a_trades

## Notes

- If `--rr` is not passed, RR is auto-calculated from entry, stop, and target1.
- `outcome_r` should be in R units (e.g., +1.0, -0.7, +2.2).
- Use `--csv` to point at a custom CSV path if required.
