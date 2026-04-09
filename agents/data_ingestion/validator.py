"""
Data Validation Layer — runs before calculate_indicators() on every OHLCV DataFrame.

Six checks:
  1. Gap detection     — missing candles vs expected interval
  2. Duplicate timestamps — drop and warn
  3. Abnormal spike    — |pct_change| > 10% in a single bar → flag
  4. Stale data        — consecutive identical OHLCV rows → WebSocket freeze
  5. Zero-volume bars  — flag for indicator masking
  6. Timezone alignment — ensure tz-aware UTC or IST

DataQualityReport.is_safe = False → caller should skip signal generation for this cycle.
DataQualityReport.quality_pct is attached to every outgoing signal as `data_quality`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

# ── Expected interval gaps per timeframe ─────────────────────────────────────
_INTERVAL_MINUTES: dict[str, int] = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}

# A symbol is considered "unsafe" when more than this fraction of rows are bad
_UNSAFE_BAD_RATIO = 0.15   # 15% bad rows → skip signal generation

# Max single-bar price move before flagging as a spike
_SPIKE_PCT = 10.0          # 10%


# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DataIssue:
    check: str          # Which check found it
    severity: str       # "warning" | "error"
    detail: str         # Human-readable description
    affected_rows: int = 0


@dataclass
class DataQualityReport:
    symbol: str
    timeframe: str
    total_rows: int
    issues: List[DataIssue] = field(default_factory=list)
    bad_rows: int = 0       # Total rows flagged as problematic
    quality_pct: float = 100.0
    is_safe: bool = True    # False → skip signal generation this cycle

    def add_issue(self, check: str, severity: str, detail: str, affected: int = 0) -> None:
        self.issues.append(DataIssue(check, severity, detail, affected))
        if severity == "error":
            self.bad_rows += affected

    def finalise(self) -> None:
        """Compute quality_pct and is_safe after all checks are done."""
        if self.total_rows > 0:
            self.quality_pct = round(
                max(0.0, (self.total_rows - self.bad_rows) / self.total_rows * 100), 1
            )
        else:
            self.quality_pct = 0.0
            self.is_safe = False
            return

        bad_ratio = self.bad_rows / self.total_rows
        self.is_safe = bad_ratio < _UNSAFE_BAD_RATIO

        if not self.is_safe:
            logger.warning(
                "[%s] Data quality too low (%.1f%%) — skipping signal generation",
                self.symbol, self.quality_pct,
            )
        elif self.issues:
            logger.debug(
                "[%s] Data quality %.1f%% — %d issue(s)",
                self.symbol, self.quality_pct, len(self.issues),
            )

    def summary_str(self) -> str:
        parts = [f"quality={self.quality_pct:.0f}%", f"safe={self.is_safe}"]
        if self.issues:
            parts.append("issues=" + "|".join(i.check for i in self.issues))
        return f"DataQuality[{self.symbol}] " + " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────

def validate(
    df: pd.DataFrame,
    symbol: str = "UNKNOWN",
    timeframe: str = "5m",
    fix_inplace: bool = True,
) -> tuple[pd.DataFrame, DataQualityReport]:
    """
    Validate an OHLCV DataFrame before indicator calculation.

    Parameters
    ----------
    df : pd.DataFrame
        Must contain columns: open, high, low, close, volume, timestamp.
        timestamp must be either a DatetimeTZDaware column or the index.
    symbol : str
        Symbol name — used only for logging.
    timeframe : str
        Expected candle interval (e.g. "5m", "1h", "1d").
    fix_inplace : bool
        If True, silently fix safe issues (duplicates, timezone).
        Spikes are capped; stale rows are flagged but not removed.

    Returns
    -------
    (cleaned_df, report) tuple.
    """
    report = DataQualityReport(symbol=symbol, timeframe=timeframe, total_rows=len(df))

    if df.empty:
        report.add_issue("empty", "error", "DataFrame is empty", 0)
        report.is_safe = False
        return df, report

    result = df.copy()

    # ── Normalise timestamp column / index ────────────────────────────────
    result = _check_timezone(result, report, fix_inplace)

    # ── Check 2: Duplicate timestamps ─────────────────────────────────────
    result = _check_duplicates(result, report, fix_inplace)

    # ── Check 1: Gap detection ────────────────────────────────────────────
    _check_gaps(result, timeframe, report)

    # ── Check 3: Abnormal price spikes ────────────────────────────────────
    result = _check_spikes(result, report, fix_inplace)

    # ── Check 3b: Corporate action shift detection (adj_close/close ratio) ─
    _check_corporate_actions(result, report)

    # ── Check 4: Stale data (frozen WebSocket rows) ───────────────────────
    _check_stale(result, report)

    # ── Check 5: Zero-volume bars ─────────────────────────────────────────
    _check_zero_volume(result, report)

    # ── Finalise ──────────────────────────────────────────────────────────
    report.total_rows = len(result)
    report.finalise()

    return result, report


# ─────────────────────────────────────────────────────────────────────────────
# Individual check helpers
# ─────────────────────────────────────────────────────────────────────────────

def _check_timezone(
    df: pd.DataFrame, report: DataQualityReport, fix: bool
) -> pd.DataFrame:
    """Check 6 — Ensure timestamp is timezone-aware."""
    ts_col = "timestamp"

    # Normalise: if timestamp is the index, reset to column temporarily
    from_index = False
    if ts_col not in df.columns and isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={"index": ts_col})
        from_index = True
    elif ts_col not in df.columns:
        report.add_issue("timezone", "warning", "No 'timestamp' column found")
        return df

    ts = df[ts_col]

    if not pd.api.types.is_datetime64_any_dtype(ts):
        try:
            df[ts_col] = pd.to_datetime(ts, utc=True)
            report.add_issue("timezone", "warning", "Timestamp parsed from string — assumed UTC")
        except Exception as exc:
            report.add_issue("timezone", "error", f"Cannot parse timestamp: {exc}", len(df))
            return df

    # Check tz-awareness
    if hasattr(df[ts_col].dtype, "tz") and df[ts_col].dtype.tz is None:
        if fix:
            df[ts_col] = df[ts_col].dt.tz_localize("UTC")
            report.add_issue("timezone", "warning", "Timestamp had no timezone — localized to UTC")
        else:
            report.add_issue("timezone", "warning", "Timestamp is timezone-naive")

    if from_index:
        df = df.set_index(ts_col)

    return df


def _check_duplicates(
    df: pd.DataFrame, report: DataQualityReport, fix: bool
) -> pd.DataFrame:
    """Check 2 — Remove duplicate timestamp rows."""
    ts_col = "timestamp"
    if ts_col not in df.columns:
        col = df.index
    else:
        col = df[ts_col]

    dups = col.duplicated(keep="first")
    n_dups = int(dups.sum())

    if n_dups > 0:
        report.add_issue(
            "duplicates", "error",
            f"{n_dups} duplicate timestamp(s) found and dropped", n_dups,
        )
        if fix:
            df = df[~dups].copy()

    return df


def _check_gaps(df: pd.DataFrame, timeframe: str, report: DataQualityReport) -> None:
    """Check 1 — Detect missing candles vs expected frequency."""
    interval_min = _INTERVAL_MINUTES.get(timeframe)
    if interval_min is None:
        return   # Unknown timeframe — skip gap check

    ts_col = "timestamp"
    if ts_col in df.columns:
        ts = pd.to_datetime(df[ts_col])
    elif isinstance(df.index, pd.DatetimeIndex):
        ts = df.index
    else:
        return

    if len(ts) < 2:
        return

    expected_delta = pd.Timedelta(minutes=interval_min)
    diffs = ts.sort_values().diff().dropna()

    # A gap is any diff > 1.5× the expected interval (accounting for weekends,
    # market-hours breaks etc. we only flag truly excessive gaps)
    gaps = diffs[diffs > expected_delta * 3]
    n_gaps = len(gaps)

    if n_gaps > 0:
        total_missing = int(
            sum(int(d.total_seconds() / 60 / interval_min) - 1 for d in gaps)
        )
        report.add_issue(
            "gaps", "warning",
            f"{n_gaps} gap(s) detected — approx {total_missing} missing candle(s)",
            min(total_missing, report.total_rows // 4),  # conservative bad_rows
        )


def _check_spikes(
    df: pd.DataFrame, report: DataQualityReport, fix: bool
) -> pd.DataFrame:
    """Check 3 — Flag and optionally cap abnormal price spikes."""
    if "close" not in df.columns:
        return df

    pct_change = df["close"].pct_change().abs() * 100
    spike_mask = pct_change > _SPIKE_PCT
    n_spikes = int(spike_mask.sum())

    if n_spikes > 0:
        report.add_issue(
            "spikes", "error",
            f"{n_spikes} bar(s) with |pct_change| > {_SPIKE_PCT}%", n_spikes,
        )
        if fix:
            # Cap close to max allowed move from previous close
            prev_close = df["close"].shift(1)
            max_close = prev_close * (1 + _SPIKE_PCT / 100)
            min_close = prev_close * (1 - _SPIKE_PCT / 100)
            df.loc[spike_mask, "close"] = df.loc[spike_mask, "close"].clip(
                lower=min_close[spike_mask],
                upper=max_close[spike_mask],
            )
            logger.debug("[%s] Capped %d spike(s)", report.symbol, n_spikes)

    return df


def _check_stale(df: pd.DataFrame, report: DataQualityReport) -> None:
    """Check 4 — Detect consecutive identical OHLCV rows (WebSocket freeze)."""
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    if len(cols) < 2:
        return

    # A row is stale when ALL of these OHLCV values are identical to the previous row
    prev = df[cols].shift(1)
    identical = (df[cols] == prev).all(axis=1)
    identical.iloc[0] = False   # First row has no previous — never stale

    n_stale = int(identical.sum())
    if n_stale > 0:
        # Only warn on consecutive stale runs (>= 3 in a row) — single repeats can be normal
        # for illiquid symbols
        runs = _count_consecutive_true(identical)
        long_runs = sum(1 for r in runs if r >= 3)
        if long_runs > 0:
            report.add_issue(
                "stale", "error",
                f"{n_stale} stale row(s) ({long_runs} consecutive run(s) of ≥3 identical bars)",
                n_stale,
            )
        else:
            report.add_issue(
                "stale", "warning",
                f"{n_stale} identical-to-previous bar(s) — possible low liquidity",
                0,
            )


def _check_corporate_actions(df: pd.DataFrame, report: DataQualityReport) -> None:
    """Detect probable splits/dividends from abrupt adj_close/close ratio shifts."""
    if "adj_close" not in df.columns or "close" not in df.columns:
        return

    base = df["close"].replace(0, np.nan)
    ratio = (df["adj_close"] / base).replace([np.inf, -np.inf], np.nan)
    if ratio.dropna().empty:
        return

    ratio_change = ratio.pct_change().abs()
    # 8%+ abrupt ratio step between bars is suspicious for corporate action.
    flagged = ratio_change > 0.08
    n_flagged = int(flagged.sum())
    if n_flagged > 0:
        report.add_issue(
            "corporate_action",
            "warning",
            f"{n_flagged} adj/close ratio jump(s) detected — possible split/dividend adjustment",
            0,
        )


def _check_zero_volume(df: pd.DataFrame, report: DataQualityReport) -> None:
    """Check 5 — Flag zero-volume bars (indicator distortion risk)."""
    if "volume" not in df.columns:
        return

    zero_vol = (df["volume"].fillna(0) == 0)
    n_zero = int(zero_vol.sum())
    if n_zero > 0:
        report.add_issue(
            "zero_volume", "warning",
            f"{n_zero} zero-volume bar(s) — may distort volume-based indicators",
            0,   # Not counted as bad rows (bars are kept, just flagged)
        )


def _count_consecutive_true(series: pd.Series) -> list[int]:
    """Return list of run lengths for consecutive True values in a boolean Series."""
    runs = []
    count = 0
    for val in series:
        if val:
            count += 1
        else:
            if count > 0:
                runs.append(count)
            count = 0
    if count > 0:
        runs.append(count)
    return runs
