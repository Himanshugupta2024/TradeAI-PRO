"""
Optional adapter for Kronos — a foundation model for financial K-lines.

Kronos (https://github.com/shiyu-coder/Kronos, MIT) is a decoder-only
transformer pre-trained on OHLCV sequences from ~45 exchanges. It forecasts a
future K-line window from a lookback window; this adapter turns those forecasts
into a small, model-agnostic feature vector that the existing signal pipeline
can consume.

Design constraints:

* **Optional.** Nothing here imports torch or Kronos at module import time.
  If the dependency is absent the rest of the app keeps working.
* **Offline.** Kronos is far too heavy for the 512 MB Render web process.
  Run it in GitHub Actions / a research box, persist the features, and let the
  API read the cached values.
* **Features, not orders.** A raw forecast is not a signal. The output is
  expected return, probability of an up move, quantiles and path dispersion —
  inputs to the existing ensemble and risk layer, never a direct trade.
* **Honest about sample size.** With ``n_paths=1`` there is no distribution:
  ``prob_up`` collapses to 0/1 and dispersion is 0. Use 8+ paths for features.

Enable with:

    pip install -r requirements-kronos.txt
    git clone https://github.com/shiyu-coder/Kronos third_party/Kronos
    python scripts/kronos_batch.py --symbols RELIANCE,TCS --out data/kronos.json
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_REPO_PATH = os.getenv("KRONOS_REPO_PATH", os.path.join("third_party", "Kronos"))
DEFAULT_MODEL_ID = os.getenv("KRONOS_MODEL_ID", "NeoQuasar/Kronos-mini")
DEFAULT_TOKENIZER_ID = os.getenv("KRONOS_TOKENIZER_ID", "NeoQuasar/Kronos-Tokenizer-2k")

MODEL_CATALOG = {
    "mini": {"model_id": "NeoQuasar/Kronos-mini", "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-2k", "context": 2048, "params": "4.1M"},
    "small": {"model_id": "NeoQuasar/Kronos-small", "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base", "context": 512, "params": "24.7M"},
    "base": {"model_id": "NeoQuasar/Kronos-base", "tokenizer_id": "NeoQuasar/Kronos-Tokenizer-base", "context": 512, "params": "102.3M"},
}

FEATURE_KEYS = [
    "kronos_expected_return_pct",
    "kronos_prob_up",
    "kronos_q05_return_pct",
    "kronos_q95_return_pct",
    "kronos_dispersion_pct",
    "kronos_path_vol_pct",
]


class KronosUnavailable(RuntimeError):
    """Raised when the optional Kronos dependency or model repo is missing."""


@dataclass
class KronosConfig:
    repo_path: str = DEFAULT_REPO_PATH
    model_id: str = DEFAULT_MODEL_ID
    tokenizer_id: str = DEFAULT_TOKENIZER_ID
    device: str = os.getenv("KRONOS_DEVICE", "cpu")
    max_context: int = 512
    lookback: int = 400
    pred_len: int = 5
    sample_count: int = 1  # paths per predict() call
    temperature: float = 1.0
    top_p: float = 0.9

    @classmethod
    def for_model(cls, size: str = "mini", **overrides) -> "KronosConfig":
        entry = MODEL_CATALOG.get(size)
        if entry is None:
            raise ValueError(f"unknown Kronos size '{size}'")
        config = cls(
            model_id=entry["model_id"],
            tokenizer_id=entry["tokenizer_id"],
            max_context=entry["context"],
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config


@dataclass
class KronosForecast:
    symbol: str
    as_of: pd.Timestamp
    pred_len: int
    n_paths: int
    last_close: float
    features: dict[str, float] = field(default_factory=dict)
    paths: Optional[np.ndarray] = None  # shape (n_paths, pred_len)

    def as_record(self) -> dict:
        record = {
            "symbol": self.symbol,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "pred_len": self.pred_len,
            "n_paths": self.n_paths,
            "last_close": self.last_close,
        }
        record.update({key: round(float(value), 4) for key, value in self.features.items()})
        return record


class KronosForecaster:
    """Load a Kronos model lazily and turn forecasts into features."""

    def __init__(
        self,
        config: Optional[KronosConfig] = None,
        predictor: Any = None,
    ):
        self.config = config or KronosConfig()
        self._predictor = predictor

    # ── availability ──────────────────────────────────────────────────────
    @staticmethod
    def is_available(repo_path: str = DEFAULT_REPO_PATH) -> bool:
        if importlib.util.find_spec("torch") is None:
            return False
        return os.path.isdir(os.path.join(os.path.abspath(repo_path), "model"))

    def load(self) -> None:
        """Import Kronos and build a predictor. Raises KronosUnavailable."""
        if self._predictor is not None:
            return

        repo = os.path.abspath(self.config.repo_path)
        if not os.path.isdir(os.path.join(repo, "model")):
            raise KronosUnavailable(
                f"Kronos repo not found at {repo}. "
                "git clone https://github.com/shiyu-coder/Kronos third_party/Kronos"
            )

        if repo not in sys.path:
            sys.path.insert(0, repo)

        try:
            from model import Kronos, KronosPredictor, KronosTokenizer  # type: ignore
        except Exception as exc:  # torch missing, broken install, ...
            raise KronosUnavailable(f"could not import Kronos: {exc}") from exc

        tokenizer = KronosTokenizer.from_pretrained(self.config.tokenizer_id)
        model = Kronos.from_pretrained(self.config.model_id)
        if self.config.device and self.config.device != "cpu":
            model = model.to(self.config.device)

        try:
            self._predictor = KronosPredictor(
                model,
                tokenizer,
                max_context=self.config.max_context,
                device=self.config.device,
            )
        except TypeError:
            # Older/newer signature without device kwarg
            self._predictor = KronosPredictor(
                model, tokenizer, max_context=self.config.max_context
            )

    # ── forecasting ───────────────────────────────────────────────────────
    def forecast(
        self,
        df: pd.DataFrame,
        symbol: str = "",
        pred_len: Optional[int] = None,
        n_paths: int = 1,
    ) -> KronosForecast:
        """Forecast ``pred_len`` bars and derive the feature vector.

        ``n_paths`` independent draws give the distribution; 1 path is a point
        forecast and should not be used for probability features.
        """
        self.load()

        pred_len = int(pred_len or self.config.pred_len)
        n_paths = max(int(n_paths), 1)

        frame = self._prepare_frame(df)
        if len(frame) < 30:
            raise ValueError("Kronos needs at least 30 bars of history")

        lookback = min(int(self.config.lookback), int(self.config.max_context), len(frame))
        history = frame.tail(lookback).reset_index(drop=True)
        last_close = float(history["close"].iloc[-1])
        as_of = pd.Timestamp(history["timestamp"].iloc[-1])

        x_df = history[["open", "high", "low", "close", "volume", "amount"]].copy()
        x_timestamp = history["timestamp"]
        y_timestamp = self._future_timestamps(history["timestamp"], pred_len)

        closes = []
        for _ in range(n_paths):
            prediction = self._predictor.predict(
                df=x_df,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=pred_len,
                T=self.config.temperature,
                top_p=self.config.top_p,
                sample_count=self.config.sample_count,
            )
            if prediction is None or len(prediction) == 0:
                raise RuntimeError("Kronos returned an empty prediction")
            closes.append(np.asarray(prediction["close"].to_numpy(dtype=float))[:pred_len])

        matrix = np.vstack(closes)
        features = self.derive_features(matrix, last_close)

        return KronosForecast(
            symbol=symbol,
            as_of=as_of,
            pred_len=pred_len,
            n_paths=n_paths,
            last_close=last_close,
            features=features,
            paths=matrix,
        )

    # ── helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def derive_features(paths: np.ndarray, last_close: float) -> dict[str, float]:
        """Turn a (n_paths × pred_len) close matrix into a feature vector."""
        matrix = np.asarray(paths, dtype=float)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        if matrix.size == 0 or not np.isfinite(last_close) or last_close <= 0:
            raise ValueError("invalid forecast matrix or last_close")

        terminal = matrix[:, -1]
        returns = (terminal / last_close - 1.0) * 100.0

        path_returns = np.diff(matrix, axis=1) / matrix[:, :-1] if matrix.shape[1] > 1 else np.zeros((matrix.shape[0], 1))
        path_vol = np.nanstd(path_returns, axis=1) * 100.0

        return {
            "kronos_expected_return_pct": float(np.mean(returns)),
            "kronos_prob_up": float(np.mean(returns > 0)),
            "kronos_q05_return_pct": float(np.quantile(returns, 0.05)),
            "kronos_q95_return_pct": float(np.quantile(returns, 0.95)),
            "kronos_dispersion_pct": float(np.std(returns)),
            "kronos_path_vol_pct": float(np.mean(path_vol)),
        }

    @staticmethod
    def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"missing OHLC columns: {sorted(missing)}")

        frame = df.copy()
        if "timestamp" not in frame.columns:
            frame["timestamp"] = pd.RangeIndex(len(frame))
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        for column in ("volume", "amount"):
            if column not in frame.columns:
                frame[column] = 0.0
            frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

    @staticmethod
    def _future_timestamps(history: pd.Series, pred_len: int) -> pd.Series:
        stamp = pd.to_datetime(history)
        if len(stamp) >= 2:
            step = stamp.iloc[-1] - stamp.iloc[-2]
        else:
            step = pd.Timedelta(days=1)
        if step <= pd.Timedelta(0):
            step = pd.Timedelta(days=1)
        return pd.Series(pd.date_range(start=stamp.iloc[-1] + step, periods=pred_len, freq=step))
