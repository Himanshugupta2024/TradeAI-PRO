"""
AI Prediction Engine — Training Pipeline.

Handles data preparation, sequence generation, normalization,
training with early stopping, class balancing, and model saving.

Supports walk-forward validation for time-series data.

Requires PyTorch — see lstm_model.py for compatibility notes.
"""

try:
    import torch
    import torch.nn as nn
    from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

import numpy as np
from utils.atomic_io import atomic_write_json
import pandas as pd
import json
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

_EPS = 1e-12

# Version stamp for the input feature contract. Bump this whenever the
# semantic meaning of FEATURE_COLS changes so that stale artifacts (pickles,
# scalers, embeddings) can be rejected instead of silently mis-fed.
FEATURE_SET_VERSION = "scale_free_v1"

# Legacy feature set: absolute price levels and raw volume. Kept for reference
# and for older artifacts only. These columns are NOT stationary — they encode
# the price level of the instrument itself, so a model trained on BTC at 20k
# extrapolates badly at 100k, and NSE rupee levels live on a completely
# different scale than crypto. Do not use them as live model inputs.
RAW_FEATURE_COLS = [
    "RSI_14", "MACD", "MACD_signal", "MACD_hist",
    "EMA_20", "EMA_50", "EMA_200", "ATR_14",
    "ADX_14", "BB_upper", "BB_lower",
    "close", "volume",
]

# Feature columns used for LSTM input — every entry is scale-free, i.e. a ratio,
# a bounded oscillator, or a z-score. This is what makes a single model usable
# across symbols and across price levels.
FEATURE_COLS = [
    "RSI_14",           # bounded 0..100 oscillator
    "MACD_pct",         # MACD / close
    "MACD_signal_pct",  # MACD signal line / close
    "MACD_hist_pct",    # MACD histogram / close
    "r_ema20",          # close / EMA20 - 1
    "r_ema50",          # close / EMA50 - 1
    "r_ema200",         # close / EMA200 - 1
    "atr_pct",          # ATR / close (dimensionless volatility)
    "ADX_14",           # bounded 0..100 trend-strength
    "bb_pos",           # 0..1 position inside the Bollinger band
    "ema_spread",       # log(EMA20 / EMA200)
    "vol_z_60",         # 60-bar volume z-score, clipped to +/-5
    "log_ret_5",        # log(close / close shifted 5 bars)
]


def _num_col(df: pd.DataFrame, name: str, index=None):
    """Return a float Series for `name`, or None when the column is absent."""
    if name not in df.columns:
        return None
    return pd.to_numeric(df[name], errors="coerce").astype(np.float64)


def _div_series(num: pd.Series, den: pd.Series) -> pd.Series:
    """Element-wise division that turns inf/zero-division into NaN, not NaN-bombs."""
    den = den.where(den.abs() > _EPS)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = num / den
    return out.replace([np.inf, -np.inf], np.nan)


def _log_ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    ratio = _div_series(num, den)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(ratio.where(ratio > 0))
    return out.replace([np.inf, -np.inf], np.nan)


def add_scale_free_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Derive the scale-free FEATURE_COLS from raw OHLCV + indicator columns.

    Missing indicator inputs are recomputed from `close`/`high`/`low`/`volume`
    with explicit `min_periods` so that warm-up rows stay NaN instead of being
    fabricated. The result always contains every column in FEATURE_COLS; NaN
    rows are the caller's problem (prepare_sequences drops them).

    This function never mutates the caller's DataFrame.
    """
    if "close" not in df.columns and "Close" not in df.columns:
        raise ValueError("add_scale_free_features: DataFrame has no 'close' column")

    out = df.copy()
    idx = out.index

    close = _num_col(out, "close")
    if close is None:
        close = _num_col(out, "Close")

    def _ema(name: str, span: int) -> pd.Series:
        existing = _num_col(out, name)
        if existing is not None:
            return existing
        return close.ewm(span=span, adjust=False, min_periods=span).mean()

    ema20 = _ema("EMA_20", 20)
    ema50 = _ema("EMA_50", 50)
    ema200 = _ema("EMA_200", 200)

    macd = _num_col(out, "MACD")
    if macd is None:
        fast = close.ewm(span=12, adjust=False, min_periods=12).mean()
        slow = close.ewm(span=26, adjust=False, min_periods=26).mean()
        macd = fast - slow
    macd_signal = _num_col(out, "MACD_signal")
    if macd_signal is None:
        macd_signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    macd_hist = _num_col(out, "MACD_hist")
    if macd_hist is None:
        macd_hist = macd - macd_signal

    atr = _num_col(out, "ATR_14")
    if atr is None:
        high = _num_col(out, "high")
        low = _num_col(out, "low")
        if high is not None and low is not None:
            prev_close = close.shift(1)
            tr = pd.concat(
                [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
                axis=1,
            ).max(axis=1)
            atr = tr.rolling(14, min_periods=14).mean()
        else:
            atr = pd.Series(np.nan, index=idx, dtype=np.float64)

    rsi = _num_col(out, "RSI_14")
    if rsi is None:
        delta = close.diff()
        gain = delta.clip(lower=0.0).rolling(14, min_periods=14).mean()
        loss = (-delta.clip(upper=0.0)).rolling(14, min_periods=14).mean()
        rs = _div_series(gain, loss)
        rsi = 100.0 - (100.0 / (1.0 + rs))

    adx = _num_col(out, "ADX_14")
    if adx is None:
        adx = pd.Series(np.nan, index=idx, dtype=np.float64)

    bb_upper = _num_col(out, "BB_upper")
    bb_lower = _num_col(out, "BB_lower")
    if bb_upper is None or bb_lower is None:
        mid = close.rolling(20, min_periods=20).mean()
        sd = close.rolling(20, min_periods=20).std(ddof=0)
        bb_upper = mid + 2.0 * sd
        bb_lower = mid - 2.0 * sd

    volume = _num_col(out, "volume")
    if volume is None:
        volume = _num_col(out, "Volume")
    if volume is None:
        volume = pd.Series(np.nan, index=idx, dtype=np.float64)
    vol_mean = volume.rolling(60, min_periods=20).mean()
    vol_std = volume.rolling(60, min_periods=20).std(ddof=0)

    out["RSI_14"] = rsi
    out["MACD_pct"] = _div_series(macd, close)
    out["MACD_signal_pct"] = _div_series(macd_signal, close)
    out["MACD_hist_pct"] = _div_series(macd_hist, close)
    out["r_ema20"] = _div_series(close, ema20) - 1.0
    out["r_ema50"] = _div_series(close, ema50) - 1.0
    out["r_ema200"] = _div_series(close, ema200) - 1.0
    out["atr_pct"] = _div_series(atr, close)
    out["ADX_14"] = adx
    out["bb_pos"] = _div_series(close - bb_lower, bb_upper - bb_lower).clip(0.0, 1.0)
    out["ema_spread"] = _log_ratio(ema20, ema200)
    out["vol_z_60"] = _div_series(volume - vol_mean, vol_std).clip(-5.0, 5.0)
    out["log_ret_5"] = _log_ratio(close, close.shift(5))

    return out


def _resolve_fit_end(scaler_fit_end, n_rows: int) -> int:
    """
    Turn `scaler_fit_end` into an absolute row index.

    `None`      -> 80% of the rows (train-side default, leaves a validation tail)
    <= 1.0      -> treated as a fraction of `n_rows`
    > 1.0       -> treated as an absolute row count
    """
    if n_rows <= 0:
        return 0
    if scaler_fit_end is None:
        return max(1, int(n_rows * 0.8))
    try:
        value = float(scaler_fit_end)
    except (TypeError, ValueError):
        return max(1, int(n_rows * 0.8))
    if value <= 1.0:
        return max(1, min(n_rows, int(n_rows * max(value, 0.0)) or 1))
    return max(1, min(n_rows, int(value)))


def fit_scaler(
    raw_features: np.ndarray,
    feature_cols: list[str],
    fit_end=None,
    label_mode: str = "next_candle_direction",
    normalize: bool = True,
) -> dict:
    """
    Fit a z-score scaler on the TRAIN PORTION of the feature matrix only.

    This is the single most important anti-leakage guard in the pipeline: the
    old implementation fitted mean/std over the entire series (including the
    validation tail), so validation metrics were computed on data that had
    informed the normalization.
    """
    X = np.asarray(raw_features, dtype=np.float64)
    n_features = len(feature_cols)

    if X.size == 0 or X.ndim != 2:
        return {
            "mean": [0.0] * n_features,
            "std": [1.0] * n_features,
            "feature_cols": list(feature_cols),
            "label_mode": label_mode,
            "normalized": bool(normalize),
            "scaler_fit_rows": 0,
            "total_rows": 0,
            "feature_set_version": FEATURE_SET_VERSION,
        }

    total_rows = int(X.shape[0])
    fit_rows = _resolve_fit_end(fit_end, total_rows)
    window = X[:fit_rows]

    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.nanmean(window, axis=0) if len(window) else np.zeros(X.shape[1])
        std = np.nanstd(window, axis=0) if len(window) else np.ones(X.shape[1])

    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where(np.isfinite(std) & (np.abs(std) > _EPS), std, 1.0)

    return {
        "mean": mean.tolist(),
        "std": std.tolist(),
        "feature_cols": list(feature_cols),
        "label_mode": label_mode,
        "normalized": bool(normalize),
        "scaler_fit_rows": int(len(window)),
        "total_rows": total_rows,
        "feature_set_version": FEATURE_SET_VERSION,
    }


def apply_scaler(X: np.ndarray, scaler_params: dict) -> np.ndarray:
    """
    Apply a fitted scaler to 2-D features or 3-D sequence batches.

    Raises instead of silently broadcasting a mismatched scaler: feeding the
    wrong mean/std is exactly the train/serve skew this pipeline is fixing.
    """
    X = np.asarray(X, dtype=np.float64)
    if X.size == 0:
        return X
    if not scaler_params or not scaler_params.get("normalized", True):
        return X

    mean = np.asarray(scaler_params.get("mean", []), dtype=np.float64)
    std = np.asarray(scaler_params.get("std", []), dtype=np.float64)
    n_features = X.shape[-1]
    if mean.size != n_features or std.size != n_features:
        raise ValueError(
            f"apply_scaler: scaler has {mean.size} features but matrix has {n_features}"
        )

    mean = np.where(np.isfinite(mean), mean, 0.0)
    std = np.where(np.isfinite(std) & (np.abs(std) > _EPS), std, 1.0)
    return (X - mean) / std


def load_persisted_scaler(
    path: str = "models/scaler_params.json",
    required_features: list[str] | None = None,
) -> dict | None:
    """
    Load a scaler artifact, returning None when it does not match the current
    feature contract. Callers must treat None as "cannot safely run this model"
    rather than fitting a fresh scaler at inference time.
    """
    from pathlib import Path

    if not path or not Path(path).exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            params = json.load(fh)
    except Exception as exc:  # pragma: no cover - corrupt artifact
        log.warning("Could not read scaler artifact %s: %s", path, exc)
        return None

    if not isinstance(params, dict) or "mean" not in params or "std" not in params:
        log.warning("Scaler artifact %s is missing mean/std", path)
        return None

    stored_version = params.get("feature_set_version")
    if stored_version != FEATURE_SET_VERSION:
        log.warning(
            "Scaler artifact %s was produced under feature set %r, current is %r. "
            "Refusing to use it - retrain to regenerate the artifact.",
            path, stored_version, FEATURE_SET_VERSION,
        )
        return None

    expected = list(required_features or FEATURE_COLS)
    stored = list(params.get("feature_cols") or [])
    if stored and stored != expected:
        log.warning(
            "Scaler artifact %s was fitted on %d feature(s) that do not match the "
            "current feature contract (%s). Refusing to use it.",
            path, len(stored), FEATURE_SET_VERSION,
        )
        return None
    if len(params["mean"]) != len(expected) or len(params["std"]) != len(expected):
        log.warning(
            "Scaler artifact %s has %d mean/std entries, expected %d",
            path, len(params["mean"]), len(expected),
        )
        return None
    return params


def _feature_matrix(clean: pd.DataFrame, columns: list[str]) -> np.ndarray:
    return clean[columns].to_numpy(dtype=np.float64)


def _windows(matrix: np.ndarray, seq_length: int):
    """Build (n_samples, seq_length, n_features) windows plus their end rows."""
    end_idx = np.arange(seq_length, len(matrix), dtype=int)
    if end_idx.size == 0:
        return np.array([]), end_idx
    X = np.stack([matrix[i - seq_length:i] for i in end_idx])
    return X, end_idx


def _clean_frame(df: pd.DataFrame, feature_cols: list[str]):
    enriched = add_scale_free_features(df)
    available = [c for c in feature_cols if c in enriched.columns]
    if not available:
        return None, []
    missing = [c for c in feature_cols if c not in available]
    if missing:
        log.warning("add_scale_free_features did not produce columns: %s", missing)
    return enriched.dropna(subset=available), available


def build_sequence_windows(
    df: pd.DataFrame,
    seq_length: int = 60,
    feature_cols: list[str] | None = None,
    scaler_params: dict | None = None,
    normalize: bool = True,
    scaler_fit_end=None,
) -> tuple:
    """
    Build normalized sequence windows with no label filtering.

    Returns (X, end_index, scaler_params) where `end_index[i]` is the DataFrame
    index label of the last bar in window `i`. Inference callers align
    embeddings back onto their feature frame with it instead of guessing a
    padding offset, which is how the previous implementation silently shifted
    every embedding by the number of dropped warm-up rows.
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLS

    clean, available = _clean_frame(df, feature_cols)
    if clean is None or len(clean) < seq_length + 1:
        return np.array([]), np.array([], dtype=int), {}

    raw = _feature_matrix(clean, available)

    if scaler_params is None:
        params = fit_scaler(raw, available, fit_end=scaler_fit_end, normalize=normalize)
    else:
        params = dict(scaler_params)
        stored = list(params.get("feature_cols") or [])
        if stored and stored != list(available):
            raise ValueError(
                "build_sequence_windows: scaler feature_cols do not match the frame"
            )
        if "normalized" in params:
            normalize = bool(params["normalized"])
        if len(params.get("mean", [])) != len(available):
            raise ValueError(
                f"build_sequence_windows: scaler has {len(params.get('mean', []))} "
                f"features, matrix has {len(available)}"
            )

    matrix = apply_scaler(raw, params) if normalize else raw
    X, end_idx = _windows(matrix, seq_length)
    if len(end_idx) == 0:
        return X, np.array([], dtype=int), params
    return X, clean.index.to_numpy()[end_idx], params


def prepare_sequences(
    df: pd.DataFrame,
    seq_length: int = 60,
    target_col: str = "close",
    feature_cols: list[str] = None,
    target_threshold: float = 0.001,
    use_outcome_labels: bool = True,
    normalize: bool = True,
    scaler_fit_end=None,
) -> tuple:
    """
    Convert a DataFrame into input/target sequences for LSTM.

    Args:
        use_outcome_labels: When True (default), uses label_1r2r_outcome() as
            the training target — i.e., did this bar produce a profitable
            1:2 risk-reward trade within OUTCOME_FORWARD_BARS candles?
            (1=WIN → maps to 1, -1=LOSS → maps to 0; 0=TIMEOUT is dropped.)
            When False, falls back to next-candle direction classification
            (1=UP, 0=DOWN) filtered by target_threshold.
        normalize: When False the raw (scale-free) values are returned and the
            scaler metadata records `normalized=False`.
        scaler_fit_end: Row boundary used to fit the scaler. `None` means the
            first 80% of rows — the validation tail never touches mean/std.
            Pass an absolute row count (>1) or a fraction (<=1) to control it.

    Returns:
        X: np.array of shape (n_samples, seq_length, n_features)
        y: np.array of shape (n_samples,) — binary 0/1
        scaler_params: dict with mean/std, feature_cols, fit boundary metadata
    """
    if feature_cols is None:
        feature_cols = FEATURE_COLS

    clean, available = _clean_frame(df, feature_cols)
    if clean is None or len(clean) < seq_length + 1:
        return np.array([]), np.array([]), {}

    raw = _feature_matrix(clean, available)
    params = fit_scaler(
        raw,
        available,
        fit_end=scaler_fit_end,
        label_mode="outcome_1r2r" if use_outcome_labels else "next_candle_direction",
        normalize=normalize,
    )
    matrix = apply_scaler(raw, params) if normalize else raw
    X_all, end_idx = _windows(matrix, seq_length)
    if len(X_all) == 0:
        return np.array([]), np.array([]), params

    keep = np.zeros(len(end_idx), dtype=bool)
    labels: list[int] = []

    if use_outcome_labels:
        from agents.feature_engine.label_generator import label_1r2r_outcome

        outcome = label_1r2r_outcome(clean).to_numpy()
        for k, i in enumerate(end_idx):
            value = int(outcome[i])
            if value == 0:
                continue  # timeout / no signal — not informative
            keep[k] = True
            labels.append(1 if value == 1 else 0)
    else:
        if target_col in clean.columns:
            closes = clean[target_col].to_numpy(dtype=np.float64)
        else:
            closes = clean["close"].to_numpy(dtype=np.float64)
        last = len(closes) - 1
        for k, i in enumerate(end_idx):
            if i >= last:
                continue
            prev = closes[i]
            if not np.isfinite(prev) or prev == 0.0:
                continue
            ret = (closes[i + 1] - prev) / prev
            if abs(ret) < target_threshold:
                continue
            keep[k] = True
            labels.append(1 if ret > 0 else 0)

    if not labels:
        return np.array([]), np.array([]), params

    return X_all[keep], np.array(labels, dtype=np.int64), params


def walk_forward_split(
    X: np.ndarray,
    y: np.ndarray,
    n_splits: int = 5,
    train_ratio: float = 0.7,
    embargo_bars: int | None = None,
) -> list[tuple]:
    """
    Walk-forward cross-validation splits for time-series data.

    Unlike k-fold, this preserves temporal ordering:
    each fold trains on older data and validates on newer data.

    Args:
        embargo_bars: Number of samples dropped between train and validation in
            every fold. Label horizon is `OUTCOME_FORWARD_BARS` bars, so a
            sample at the end of the train slice has a label that was resolved
            using prices inside the validation slice -- that is look-ahead
            leakage. Dropping the last `embargo_bars` training samples removes
            it. `None` lazily reads `config.settings.OUTCOME_FORWARD_BARS`.

    Returns:
        List of (X_train, y_train, X_val, y_val) tuples
    """
    if embargo_bars is None:
        try:
            from config.settings import OUTCOME_FORWARD_BARS

            embargo_bars = int(OUTCOME_FORWARD_BARS)
        except Exception:  # pragma: no cover - settings import is best effort
            embargo_bars = 0
    embargo_bars = max(0, int(embargo_bars))

    n = len(X)
    fold_size = n // n_splits
    splits = []

    for i in range(n_splits):
        val_end = n - (n_splits - i - 1) * fold_size
        val_start = val_end - fold_size
        train_end = max(0, val_start - embargo_bars)
        train_start = max(0, train_end - int(fold_size * train_ratio / (1 - train_ratio)))

        if train_end - train_start < 10 or val_end - val_start < 5:
            continue

        splits.append((
            X[train_start:train_end],
            y[train_start:train_end],
            X[val_start:val_end],
            y[val_start:val_end],
        ))

    return splits


if TORCH_AVAILABLE:

    class SequenceDataset(Dataset):
        """PyTorch Dataset for LSTM sequences."""

        def __init__(self, X: np.ndarray, y: np.ndarray):
            self.X = torch.FloatTensor(X)
            self.y = torch.LongTensor(y)

        def __len__(self):
            return len(self.X)

        def __getitem__(self, idx):
            return self.X[idx], self.y[idx]


    def train_model(
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        epochs: int = 50,
        batch_size: int = 64,
        learning_rate: float = 0.001,
        patience: int = 8,
        save_path: str = "models/",
        random_seed: int = 42,
    ) -> dict:
        """
        Train the LSTM model with early stopping and class balancing.

        Uses WeightedRandomSampler to handle class imbalance
        (markets often have more UP than DOWN periods or vice versa).

        Args:
            random_seed: Fixed seed for NumPy, Python random, and PyTorch so that
                repeated training runs on the same data produce stable, comparable
                metrics. Set to None to disable determinism.
        """
        from agents.ai_engine.lstm_model import TradingLSTM

        # ── Deterministic seeding ────────────────────────────────────────────
        if random_seed is not None:
            import random as _random
            _random.seed(random_seed)
            np.random.seed(random_seed)
            torch.manual_seed(random_seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(random_seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[AI] Training on: {device}")

        input_size = X_train.shape[2]
        model = TradingLSTM(input_size=input_size).to(device)

        train_dataset = SequenceDataset(X_train, y_train)
        val_dataset = SequenceDataset(X_val, y_val)

        # Class balancing via weighted sampler
        class_counts = np.bincount(y_train.astype(int), minlength=2)
        class_weights = 1.0 / np.maximum(class_counts, 1).astype(np.float64)
        sample_weights = class_weights[y_train.astype(int)]
        sampler = WeightedRandomSampler(
            weights=torch.DoubleTensor(sample_weights),
            num_samples=len(sample_weights),
            replacement=True,
        )

        print(f"[AI] Class distribution: DOWN={class_counts[0]}, UP={class_counts[1]}")

        train_loader = DataLoader(train_dataset, batch_size=batch_size, sampler=sampler)
        val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

        # Also use class-weighted loss
        loss_weights = torch.FloatTensor(class_weights / class_weights.sum()).to(device)
        criterion = nn.CrossEntropyLoss(weight=loss_weights)
        optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=1e-5)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            optimizer, mode="min", factor=0.5, patience=3
        )

        best_val_loss = float("inf")
        epochs_no_improve = 0
        history = {"train_loss": [], "val_loss": [], "val_accuracy": []}

        for epoch in range(epochs):
            # ── Training ──
            model.train()
            train_loss = 0.0
            for X_batch, y_batch in train_loader:
                X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                optimizer.zero_grad()
                outputs = model(X_batch)
                loss = criterion(outputs, y_batch)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                train_loss += loss.item()

            train_loss /= len(train_loader)

            # ── Validation ──
            model.eval()
            val_loss = 0.0
            correct = 0
            total = 0
            with torch.no_grad():
                for X_batch, y_batch in val_loader:
                    X_batch, y_batch = X_batch.to(device), y_batch.to(device)
                    outputs = model(X_batch)
                    loss = criterion(outputs, y_batch)
                    val_loss += loss.item()
                    _, predicted = torch.max(outputs, 1)
                    total += y_batch.size(0)
                    correct += (predicted == y_batch).sum().item()

            val_loss /= len(val_loader)
            val_acc = correct / total * 100

            history["train_loss"].append(train_loss)
            history["val_loss"].append(val_loss)
            history["val_accuracy"].append(val_acc)

            scheduler.step(val_loss)

            print(
                f"  Epoch {epoch+1:>3}/{epochs} | "
                f"Train Loss: {train_loss:.4f} | "
                f"Val Loss: {val_loss:.4f} | "
                f"Val Acc: {val_acc:.1f}%"
            )

            # ── Early Stopping ──
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                epochs_no_improve = 0
                os.makedirs(save_path, exist_ok=True)
                torch.save(model.state_dict(), os.path.join(save_path, "best_model.pt"))
                # Save metadata for reproducibility
                meta = {
                    "epoch": epoch + 1,
                    "val_loss": val_loss,
                    "val_accuracy": val_acc,
                    "input_size": input_size,
                    "class_distribution": class_counts.tolist(),
                    "trained_at": datetime.now().isoformat(),
                    "random_seed": random_seed,
                    "batch_size": batch_size,
                    "learning_rate": learning_rate,
                }
                atomic_write_json(os.path.join(save_path, "model_meta.json"), meta, indent=2)
            else:
                epochs_no_improve += 1
                if epochs_no_improve >= patience:
                    print(f"  Early stopping at epoch {epoch+1}")
                    break

        return {
            "best_val_loss": best_val_loss,
            "best_val_accuracy": max(history["val_accuracy"]),
            "epochs_trained": len(history["train_loss"]),
            "history": history,
        }

else:
    def train_model(*args, **kwargs):
        raise ImportError("PyTorch not available. Use fallback_predictor instead.")
