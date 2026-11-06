"""
Meta-labelling: predict whether a primary signal will reach its target.

The primary rule decides direction (EMA trend + momentum by default). The
meta-model answers a different question: *given this signal and its context,
what is the probability it hits the target barrier before the stop?*

Why a separate model:

* direction and timing are different problems;
* a mediocre signal can still be profitable if you only take its best subset;
* position sizing should depend on confidence, not on the primary rule's
  arbitrary score.

Design choices kept deliberately boring:

* logistic regression with L2, implemented in numpy (no sklearn dependency);
* chronological train/validation/test split, no shuffling;
* triple-barrier labels (stop wins on a same-bar touch);
* evaluation includes an economic metric, not just AUC.

Nothing here places orders. The output is a probability and a threshold.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from agents.feature_engine.label_generator import triple_barrier_labels

logger = logging.getLogger(__name__)

BASELINE_FEATURES = [
    "ret_1",
    "ret_5",
    "ret_20",
    "atr_pct",
    "rsi_14",
    "ema20_dist",
    "ema50_dist",
    "vol_z",
    "range_pct",
    "gap_pct",
    "trend",
]

KRONOS_FEATURES = [
    "kronos_expected_return_pct",
    "kronos_prob_up",
    "kronos_q05_return_pct",
    "kronos_q95_return_pct",
    "kronos_dispersion_pct",
    "kronos_path_vol_pct",
]


# ── features ────────────────────────────────────────────────────────────────
def compute_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Add price/volume features and ATR to a single-symbol OHLCV frame."""
    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"missing OHLC columns: {sorted(missing)}")

    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")
    open_ = pd.to_numeric(out["open"], errors="coerce")

    out["ret_1"] = close.pct_change()
    out["ret_5"] = close.pct_change(5)
    out["ret_20"] = close.pct_change(20)

    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    out["ATR"] = true_range.rolling(14).mean()
    out["atr_pct"] = out["ATR"] / close

    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi_14"] = 100.0 - (100.0 / (1.0 + rs))

    out["ema20"] = close.ewm(span=20, adjust=False).mean()
    out["ema50"] = close.ewm(span=50, adjust=False).mean()
    out["ema20_dist"] = close / out["ema20"] - 1.0
    out["ema50_dist"] = close / out["ema50"] - 1.0

    volume = pd.to_numeric(out.get("volume", 0), errors="coerce").fillna(0.0)
    rolling_mean = volume.rolling(20).mean()
    rolling_std = volume.rolling(20).std().replace(0, np.nan)
    out["vol_z"] = (volume - rolling_mean) / rolling_std

    out["range_pct"] = (high - low) / close
    out["gap_pct"] = open_ / prev_close - 1.0
    out["trend"] = (out["ema20"] > out["ema50"]).astype(float)

    return out


def primary_signal(frame: pd.DataFrame) -> pd.Series:
    """Simple, explainable primary rule: trend + short-term momentum."""
    signal = pd.Series("HOLD", index=frame.index, dtype=object)
    long_mask = (
        (frame["close"] > frame["ema50"])
        & (frame["ema20"] > frame["ema50"])
        & (frame["ret_5"] > 0)
    )
    short_mask = (
        (frame["close"] < frame["ema50"])
        & (frame["ema20"] < frame["ema50"])
        & (frame["ret_5"] < 0)
    )
    signal[long_mask] = "LONG"
    signal[short_mask] = "SHORT"
    return signal


# ── dataset ─────────────────────────────────────────────────────────────────
def build_meta_dataset(
    panel: pd.DataFrame,
    kronos: Optional[pd.DataFrame] = None,
    max_bars: int = 10,
    sl_atr_mult: float = 1.0,
    rr_ratio: float = 2.0,
    cost_pct: float = 0.39,
) -> pd.DataFrame:
    """Label primary signals with the triple-barrier outcome.

    Returns one row per actionable signal that hit a barrier (timeouts are
    dropped — they carry no win/loss information for this model).
    """
    if panel is None or panel.empty:
        raise ValueError("panel is empty")

    frames = []
    for symbol, group in panel.groupby("symbol"):
        group = group.copy().sort_values("timestamp")
        if len(group) < 60:
            continue
        group = compute_features(group)

        if "signal" in group.columns:
            signal = group["signal"].astype(str).str.upper()
        else:
            signal = primary_signal(group)
        group["signal"] = signal

        barriers = triple_barrier_labels(
            group,
            side="auto",
            max_bars=max_bars,
            sl_atr_mult=sl_atr_mult,
            rr_ratio=rr_ratio,
            atr_col="ATR",
        )
        group = pd.concat([group, barriers], axis=1)
        group["symbol"] = str(symbol)

        actionable = signal.isin(["LONG", "SHORT"]) & group["tb_label"].isin([1.0, -1.0])
        frames.append(group.loc[actionable])

    if not frames:
        raise ValueError("no labelled signals — check the panel and barriers")

    data = pd.concat(frames, ignore_index=True)
    data["meta_label"] = (data["tb_label"] == 1.0).astype(int)
    data["net_return_pct"] = data["tb_return_pct"] - float(cost_pct)

    feature_columns = list(BASELINE_FEATURES)
    if kronos is not None and not kronos.empty:
        data = _merge_kronos(data, kronos)
        feature_columns += KRONOS_FEATURES + ["kronos_available"]

    data = data.dropna(subset=[column for column in BASELINE_FEATURES if column in data.columns])
    data = data.reset_index(drop=True)
    data.attrs["feature_columns"] = feature_columns
    return data


def _merge_kronos(data: pd.DataFrame, kronos: pd.DataFrame) -> pd.DataFrame:
    """Attach Kronos features by symbol + date; missing rows become 0/flag 0."""
    k = kronos.copy()
    if "as_of" in k.columns:
        k["date"] = pd.to_datetime(k["as_of"], errors="coerce").dt.normalize()
    elif "date" in k.columns:
        k["date"] = pd.to_datetime(k["date"], errors="coerce").dt.normalize()
    else:
        raise ValueError("kronos frame needs an 'as_of' or 'date' column")

    k["symbol"] = k["symbol"].astype(str).str.upper()
    present = [column for column in KRONOS_FEATURES if column in k.columns]
    k = k[["symbol", "date"] + present].drop_duplicates(["symbol", "date"], keep="last")

    out = data.copy()
    out["date"] = pd.to_datetime(out["timestamp"], errors="coerce").dt.normalize()
    out["symbol"] = out["symbol"].astype(str).str.upper()
    out = out.merge(k, on=["symbol", "date"], how="left")

    for column in present:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out["kronos_available"] = out[present].notna().any(axis=1).astype(float) if present else 0.0
    for column in present:
        out[column] = out[column].fillna(0.0)
    for column in KRONOS_FEATURES:
        if column not in out.columns:
            out[column] = 0.0
    return out


# ── split ───────────────────────────────────────────────────────────────────
@dataclass
class Split:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame


def time_split(data: pd.DataFrame, val_fraction: float = 0.15, test_fraction: float = 0.15) -> Split:
    """Chronological split by calendar date. Never shuffles."""
    if data.empty:
        raise ValueError("cannot split an empty dataset")

    dates = np.sort(pd.to_datetime(data["timestamp"]).dt.normalize().unique())
    if len(dates) < 10:
        raise ValueError("need at least 10 distinct dates to split")

    test_start = dates[int(len(dates) * (1.0 - test_fraction))]
    val_start = dates[int(len(dates) * (1.0 - test_fraction - val_fraction))]

    stamps = pd.to_datetime(data["timestamp"]).dt.normalize()
    train = data[stamps < val_start]
    validation = data[(stamps >= val_start) & (stamps < test_start)]
    test = data[stamps >= test_start]

    if train.empty or validation.empty or test.empty:
        raise ValueError("split produced an empty partition")

    return Split(train=train.reset_index(drop=True),
                 validation=validation.reset_index(drop=True),
                 test=test.reset_index(drop=True))


# ── model ───────────────────────────────────────────────────────────────────
class Standardizer:
    def __init__(self):
        self.mean = None
        self.std = None

    def fit(self, X: np.ndarray) -> "Standardizer":
        self.mean = np.nanmean(X, axis=0)
        self.std = np.nanstd(X, axis=0)
        self.std[self.std < 1e-9] = 1.0
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean) / self.std


class LogisticModel:
    """L2-regularised logistic regression via full-batch gradient descent."""

    def __init__(self, l2: float = 1.0, learning_rate: float = 0.1, epochs: int = 400):
        self.l2 = float(l2)
        self.learning_rate = float(learning_rate)
        self.epochs = int(epochs)
        self.weights: Optional[np.ndarray] = None
        self.bias: float = 0.0
        self.scaler = Standardizer()

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        if len(np.unique(y)) < 2:
            raise ValueError("training labels contain a single class")

        Xs = self.scaler.fit(X).transform(X)
        n, d = Xs.shape
        weights = np.zeros(d)
        bias = 0.0

        for _ in range(self.epochs):
            logits = Xs @ weights + bias
            probs = _sigmoid(logits)
            error = probs - y
            grad_w = (Xs.T @ error) / n + (self.l2 / n) * weights
            grad_b = float(np.mean(error))
            weights -= self.learning_rate * grad_w
            bias -= self.learning_rate * grad_b

        self.weights = weights
        self.bias = float(bias)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("model is not fitted")
        Xs = self.scaler.transform(np.asarray(X, dtype=float))
        return _sigmoid(Xs @ self.weights + self.bias)

    def as_dict(self, features: list[str]) -> dict:
        weights = [] if self.weights is None else self.weights
        mean = [] if self.scaler.mean is None else self.scaler.mean
        std = [] if self.scaler.std is None else self.scaler.std
        return {
            "type": "logistic",
            "features": features,
            "weights": [float(w) for w in weights],
            "bias": float(self.bias),
            "mean": [float(m) for m in mean],
            "std": [float(s) for s in std],
            "l2": self.l2,
        }


def _sigmoid(values: np.ndarray) -> np.ndarray:
    values = np.clip(values, -30.0, 30.0)
    return 1.0 / (1.0 + np.exp(-values))


# ── metrics ─────────────────────────────────────────────────────────────────
def roc_auc(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Rank-based AUC (Mann-Whitney U), NaN-safe."""
    y_true = np.asarray(y_true, dtype=float)
    scores = np.asarray(scores, dtype=float)
    mask = np.isfinite(scores)
    y_true, scores = y_true[mask], scores[mask]
    positives = int(y_true.sum())
    negatives = int(len(y_true) - positives)
    if positives == 0 or negatives == 0:
        return float("nan")

    order = np.argsort(scores)
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)
    # Average ranks for ties
    sorted_scores = scores[order]
    i = 0
    while i < len(sorted_scores):
        j = i
        while j + 1 < len(sorted_scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1

    rank_sum_pos = ranks[y_true == 1].sum()
    return float((rank_sum_pos - positives * (positives + 1) / 2.0) / (positives * negatives))


def brier_score(y_true: np.ndarray, probs: np.ndarray) -> float:
    return float(np.mean((np.asarray(probs) - np.asarray(y_true)) ** 2))


def log_loss(y_true: np.ndarray, probs: np.ndarray, eps: float = 1e-9) -> float:
    p = np.clip(np.asarray(probs), eps, 1 - eps)
    y = np.asarray(y_true)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_table(y_true: np.ndarray, probs: np.ndarray, bins: int = 10) -> list[dict]:
    edges = np.linspace(0.0, 1.0, bins + 1)
    rows = []
    for low, high in zip(edges[:-1], edges[1:]):
        mask = (probs >= low) & (probs < high if high < 1.0 else probs <= high)
        if not mask.any():
            continue
        rows.append({
            "bin": f"{low:.1f}-{high:.1f}",
            "n": int(mask.sum()),
            "predicted": round(float(np.mean(probs[mask])), 4),
            "actual": round(float(np.mean(y_true[mask])), 4),
        })
    return rows


def auc_p_value(y_true: np.ndarray, scores: np.ndarray, n_permutations: int = 200, seed: int = 0) -> float:
    """Permutation test: how often does a random labelling beat this AUC?"""
    observed = roc_auc(y_true, scores)
    if not np.isfinite(observed):
        return 1.0
    y = np.asarray(y_true)
    rng = np.random.default_rng(seed)
    at_least = 0
    for _ in range(n_permutations):
        shuffled = rng.permutation(y)
        if roc_auc(shuffled, scores) >= observed:
            at_least += 1
    return float((at_least + 1) / (n_permutations + 1))


def select_threshold(
    y_true: np.ndarray,
    probs: np.ndarray,
    returns: np.ndarray,
    min_selected: int = 10,
    min_threshold: float = 0.05,
    max_selected_share: float = 0.7,
) -> float:
    """Pick the probability cut that maximises mean net return on validation.

    A threshold that selects almost everything is not a filter — it is the
    primary signal wearing a costume. Candidates below ``min_threshold`` or
    above ``max_selected_share`` are ignored.
    """
    best_threshold = 0.5
    best_value = -np.inf
    for threshold in np.unique(np.round(probs, 3)):
        if threshold < min_threshold:
            continue
        mask = probs >= threshold
        if mask.sum() < min_selected:
            continue
        if float(mask.mean()) > max_selected_share:
            continue
        value = float(np.mean(returns[mask]))
        if value > best_value:
            best_value = value
            best_threshold = float(threshold)
    return best_threshold


@dataclass
class MetaModelResult:
    model: LogisticModel
    features: list[str]
    threshold: float
    metrics: dict = field(default_factory=dict)
    calibration: list = field(default_factory=list)
    verdict: str = ""


def train_meta_model(
    data: pd.DataFrame,
    features: Optional[list[str]] = None,
    val_fraction: float = 0.15,
    test_fraction: float = 0.15,
    l2: float = 1.0,
    threshold: Optional[float] = None,
) -> MetaModelResult:
    """Fit the meta-model and evaluate it on a held-out chronological test set."""
    features = features or data.attrs.get("feature_columns") or BASELINE_FEATURES
    features = [column for column in features if column in data.columns]

    split = time_split(data, val_fraction=val_fraction, test_fraction=test_fraction)

    train = split.train.dropna(subset=["meta_label"])
    validation = split.validation.dropna(subset=["meta_label"])
    test = split.test.dropna(subset=["meta_label"])

    X_train = train[features].to_numpy(dtype=float)
    y_train = train["meta_label"].to_numpy(dtype=int)

    model = LogisticModel(l2=l2).fit(X_train, y_train)

    val_probs = model.predict_proba(validation[features].to_numpy(dtype=float))
    chosen_threshold = (
        float(threshold)
        if threshold is not None
        else select_threshold(
            validation["meta_label"].to_numpy(dtype=int),
            val_probs,
            validation["net_return_pct"].to_numpy(dtype=float),
        )
    )

    test_probs = model.predict_proba(test[features].to_numpy(dtype=float))
    y_test = test["meta_label"].to_numpy(dtype=int)
    returns = test["net_return_pct"].to_numpy(dtype=float)

    selected = test_probs >= chosen_threshold
    selected_returns = returns[selected]

    metrics = {
        "train_rows": int(len(train)),
        "validation_rows": int(len(validation)),
        "test_rows": int(len(test)),
        "positive_rate": round(float(y_test.mean()), 4) if len(y_test) else 0.0,
        "auc": round(roc_auc(y_test, test_probs), 4),
        "auc_p_value": round(auc_p_value(y_test, test_probs), 4),
        "brier": round(brier_score(y_test, test_probs), 4),
        "log_loss": round(log_loss(y_test, test_probs), 4),
        "threshold": round(chosen_threshold, 3),
        "selected": int(selected.sum()),
        "selected_share": round(float(selected.mean()), 3) if len(selected) else 0.0,
        "precision_selected": round(float(y_test[selected].mean()), 4) if selected.any() else 0.0,
        "expectancy_all_pct": round(float(np.mean(returns)), 4) if len(returns) else 0.0,
        "expectancy_selected_pct": round(float(np.mean(selected_returns)), 4) if len(selected_returns) else 0.0,
        "win_rate_selected": round(float((selected_returns > 0).mean()), 4) if len(selected_returns) else 0.0,
    }
    metrics["lift_pct"] = round(
        metrics["expectancy_selected_pct"] - metrics["expectancy_all_pct"], 4
    )

    edge = (
        np.isfinite(metrics["auc"])
        and metrics["auc"] >= 0.55
        and metrics["auc_p_value"] < 0.05
        and metrics["lift_pct"] > 0.05
        and metrics["selected"] >= 20
        and 0.05 <= metrics["selected_share"] <= 0.70
        and metrics["precision_selected"] > metrics["positive_rate"] + 0.05
    )
    verdict = (
        "EDGE: candidate for shadow mode"
        if edge
        else "NO EDGE: do not size positions with this model yet"
    )

    return MetaModelResult(
        model=model,
        features=features,
        threshold=chosen_threshold,
        metrics=metrics,
        calibration=calibration_table(y_test, test_probs),
        verdict=verdict,
    )


def save_result(result: MetaModelResult, model_path: str, metrics_path: Optional[str] = None) -> None:
    payload = {
        "model": result.model.as_dict(result.features),
        "threshold": result.threshold,
        "verdict": result.verdict,
        "metrics": result.metrics,
        "calibration": result.calibration,
    }
    with open(model_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    if metrics_path:
        with open(metrics_path, "w", encoding="utf-8") as handle:
            json.dump({"metrics": result.metrics, "verdict": result.verdict}, handle, indent=2)
