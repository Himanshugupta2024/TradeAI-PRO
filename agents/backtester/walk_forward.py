"""
Walk-Forward Validation for backtesting.

Prevents overfitting by splitting data into rolling train/test windows
that simulate real-world sequential deployment.
"""

import pandas as pd
from dataclasses import dataclass

from agents.backtester.engine import Backtester, BacktestResult


@dataclass
class WalkForwardWindow:
    """A single train/test split window."""
    window_id: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_rows: int
    test_rows: int
    test_result: BacktestResult = None


@dataclass
class WalkForwardResult:
    """Aggregated walk-forward results across all windows."""
    windows: list[WalkForwardWindow]
    combined_result: BacktestResult

    def summary(self) -> str:
        lines = [
            f"\n{'=' * 60}",
            f"  WALK-FORWARD VALIDATION ({len(self.windows)} windows)",
            f"{'=' * 60}",
        ]
        for w in self.windows:
            wr = w.test_result
            lines.append(
                f"  Window {w.window_id}: "
                f"Test {w.test_start.date()}→{w.test_end.date()} | "
                f"Trades: {wr.total_trades:>3} | "
                f"WR: {wr.win_rate:>5.1f}% | "
                f"PnL: {wr.total_pnl:>+10.2f} | "
                f"Sharpe: {wr.sharpe_ratio:>5.2f}"
            )

        lines.append(f"\n  --- Combined OOS Results ---")
        lines.append(self.combined_result.summary())
        return "\n".join(lines)


def generate_windows(
    df: pd.DataFrame,
    train_days: int = 365,
    test_days: int = 90,
    step_days: int = 90,
) -> list[tuple[pd.DataFrame, pd.DataFrame]]:
    """
    Generate walk-forward train/test DataFrame splits.

    Args:
        df: Full dataset with 'timestamp' column
        train_days: Number of days in each training window
        test_days: Number of days in each test window
        step_days: How many days to slide forward between windows

    Returns:
        List of (train_df, test_df) tuple pairs
    """
    if df.empty or "timestamp" not in df.columns:
        return []

    df = df.sort_values("timestamp").reset_index(drop=True)
    start = df["timestamp"].iloc[0]
    end = df["timestamp"].iloc[-1]

    windows = []
    cursor = start

    while True:
        train_start = cursor
        train_end = train_start + pd.Timedelta(days=train_days)
        test_start = train_end
        test_end = test_start + pd.Timedelta(days=test_days)

        if test_end > end:
            # Try to fit a final window using remaining data
            if test_start < end:
                test_end = end
            else:
                break

        train_df = df[(df["timestamp"] >= train_start) & (df["timestamp"] < train_end)]
        test_df = df[(df["timestamp"] >= test_start) & (df["timestamp"] < test_end)]

        if not train_df.empty and not test_df.empty:
            windows.append((train_df.copy(), test_df.copy()))

        cursor += pd.Timedelta(days=step_days)

        if cursor + pd.Timedelta(days=train_days) > end:
            break

    return windows


def run_walk_forward(
    df: pd.DataFrame,
    signal_generator,
    backtester: Backtester = None,
    train_days: int = 365,
    test_days: int = 90,
    step_days: int = 90,
) -> WalkForwardResult:
    """
    Run walk-forward validation.

    The signal_generator is a callable:
        signal_generator(train_df, test_df) -> test_df_with_signals

    It receives the training data (to optimize/train on) and the test data,
    and must return the test_df with 'signal', 'stop_loss', and 'target' columns populated.

    Args:
        df: Full historical DataFrame
        signal_generator: Callable(train_df, test_df) -> test_df_with_signals
        backtester: Backtester instance (uses defaults if None)
        train_days: Training window size
        test_days: Test window size
        step_days: Step between windows

    Returns:
        WalkForwardResult with per-window and combined metrics
    """
    if backtester is None:
        backtester = Backtester()

    windows = generate_windows(df, train_days, test_days, step_days)

    if not windows:
        return WalkForwardResult(windows=[], combined_result=BacktestResult())

    wf_windows = []
    all_trades = []

    for idx, (train_df, test_df) in enumerate(windows):
        # Let the signal generator use training data to calibrate,
        # then produce signals on the test set
        test_with_signals = signal_generator(train_df, test_df)

        # Run backtest on the out-of-sample test set
        result = backtester.run(test_with_signals)

        wf_window = WalkForwardWindow(
            window_id=idx + 1,
            train_start=train_df["timestamp"].iloc[0],
            train_end=train_df["timestamp"].iloc[-1],
            test_start=test_df["timestamp"].iloc[0],
            test_end=test_df["timestamp"].iloc[-1],
            train_rows=len(train_df),
            test_rows=len(test_df),
            test_result=result,
        )
        wf_windows.append(wf_window)
        all_trades.extend(result.trades)

    # Compute combined OOS metrics
    combined = backtester._compute_metrics(all_trades)

    return WalkForwardResult(windows=wf_windows, combined_result=combined)
