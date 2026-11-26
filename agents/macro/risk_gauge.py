"""
Market risk-on/risk-off gauge using VIX, BTC, Nifty, USD/JPY, and Gold.

Measures the global risk environment by analyzing multiple risk indicators:
- VIX: Market volatility (fear gauge)
- BTC: Crypto risk appetite
- Nifty: Indian equity market
- USD/JPY: Safe-haven currency flows
- Gold: Traditional safe-haven asset

Risk-On: VIX low, BTC rising, Nifty rising, USD/JPY falling, Gold falling
Risk-Off: VIX high, BTC falling, Nifty falling, USD/JPY rising, Gold rising

Implements 1-hour cache with fallback to stale data.
"""

import json
import logging
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Literal

import requests
import yfinance as yf

from config.settings import (
    EDGE_RISK_CACHE_FILE,
    EDGE_RISK_CACHE_TTL,
    EDGE_FALLBACK_ENABLED,
    TWELVE_DATA_API_KEY,
)

logger = logging.getLogger(__name__)


@dataclass
class RiskIndicators:
    """Market risk indicators"""
    vix: Optional[float]                   # VIX index (volatility)
    vix_change_pct: Optional[float]        # % change in VIX (7-day)
    vix_1d_change_pct: Optional[float] = None # % change in VIX (1-day)
    btc_trend: Optional[str] = None        # "RISING" | "FALLING" | "FLAT"
    nifty_trend: Optional[str] = None      # "RISING" | "FALLING" | "FLAT"
    usdjpy_level: Optional[float] = None   # USD/JPY rate
    usdjpy_trend: Optional[str] = None     # "RISING" | "FALLING" | "FLAT"
    gold_trend: Optional[str] = None       # "RISING" | "FALLING" | "FLAT"
    risk_regime: str = "NEUTRAL"           # "RISK_ON" | "RISK_OFF" | "NEUTRAL"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Enhanced Indian Market & Breadth indicators
    india_vix: Optional[float] = None
    india_vix_change_pct: Optional[float] = None  # 1-day change %
    india_vix_7d_change_pct: Optional[float] = None  # 7-day change %
    india_vix_trend: Optional[str] = None
    ad_ratio: Optional[float] = None
    above_ema20_pct: Optional[float] = None
    breadth_regime: Optional[str] = None
    # Risk Guardrail Vetoes & Posture
    posture: str = "OPEN"                  # "OPEN" | "SELECTIVE" | "STAND_DOWN"
    vetoes: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    data_integrity: str = "CONFIRMED"     # "CONFIRMED" | "UNCONFIRMED_VOL_SPIKE" | "VIX_SOURCE_STALE" | "VOL_SPIKE_EXPIRED"
    vix_fetched_at: Optional[datetime] = None
    india_vix_fetched_at: Optional[datetime] = None
    spike_flag_time: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization"""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        if self.vix_fetched_at:
            data["vix_fetched_at"] = self.vix_fetched_at.isoformat()
        if self.india_vix_fetched_at:
            data["india_vix_fetched_at"] = self.india_vix_fetched_at.isoformat()
        if self.spike_flag_time:
            data["spike_flag_time"] = self.spike_flag_time.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RiskIndicators":
        """Create from dict (JSON deserialization with field filtering)"""
        d = dict(data)
        ts = datetime.fromisoformat(d["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        d["timestamp"] = ts
        for dt_field in ("vix_fetched_at", "india_vix_fetched_at", "spike_flag_time"):
            if d.get(dt_field):
                dt_val = datetime.fromisoformat(d[dt_field])
                if dt_val.tzinfo is None:
                    dt_val = dt_val.replace(tzinfo=timezone.utc)
                d[dt_field] = dt_val
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


_EDGE_MEM_CACHE_LOCK = threading.Lock()
_EDGE_MEM_CACHE: dict[str, tuple[float, "RiskIndicators"]] = {}
_EDGE_FETCH_LOCKS_LOCK = threading.Lock()
_EDGE_FETCH_LOCKS: dict[str, threading.Lock] = {}


def _get_risk_fetch_lock(cache_key: str) -> threading.Lock:
    with _EDGE_FETCH_LOCKS_LOCK:
        lock = _EDGE_FETCH_LOCKS.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _EDGE_FETCH_LOCKS[cache_key] = lock
        return lock


class RiskGauge:
    """Measures market risk environment"""

    # VIX thresholds (Global US VIX)
    VIX_LOW_THRESHOLD = 15.0    # VIX < 15 = low volatility (risk-on)
    VIX_HIGH_THRESHOLD = 30.0   # VIX > 30 = high volatility (risk-off)

    # India VIX thresholds (NSE Local Equities)
    INDIA_VIX_LOW_THRESHOLD = 13.0   # India VIX < 13 = Volatility compression
    INDIA_VIX_HIGH_THRESHOLD = 22.0  # India VIX > 22 = High volatility turbulence

    # Trend calculation parameters
    TREND_MA_PERIOD = 7         # 7-day moving average
    TREND_THRESHOLD_PCT = 2.0   # 2% threshold for trend classification

    # Minimum indicators required
    MIN_INDICATORS = 3          # Need at least 3 of indicators

    # Indicator Weights for Indian Market Desk (Sum = 100)
    WEIGHTS = {
        "india_vix": 18.0,
        "india_vix_velocity": 12.0,
        "vix_shock": 10.0,
        "ad_ratio": 16.0,
        "above_ema20": 12.0,
        "us_vix": 10.0,
        "nifty_trend": 10.0,
        "btc_trend": 5.0,
        "usdjpy_trend": 4.0,
        "gold_trend": 3.0,
    }

    def __init__(
        self,
        cache_file: str = EDGE_RISK_CACHE_FILE,
        cache_ttl_seconds: int = EDGE_RISK_CACHE_TTL,
        fallback_enabled: bool = EDGE_FALLBACK_ENABLED,
    ) -> None:
        """
        Initialize risk gauge.

        Args:
            cache_file: Path to cache file
            cache_ttl_seconds: Cache TTL (default 1 hour)
            fallback_enabled: Use stale cache on API failure
        """
        self.cache_file = Path(cache_file)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fallback_enabled = fallback_enabled

    def calculate_component_scores(self, indicators: RiskIndicators) -> dict[str, dict]:
        """
        Calculate individual component scores using graded severity curves.
        Returns dict: {component_key: {'score': float, 'weight': float, 'value': any}}
        """
        components: dict[str, dict] = {}

        # 1. India VIX Level (Local NSE Volatility - 18% weight)
        if getattr(indicators, "india_vix", None) is not None:
            iv = indicators.india_vix
            if iv <= 13.0:
                s = 100.0
            elif iv >= 24.0:
                s = -100.0
            elif iv <= 16.0:
                s = ((16.0 - iv) / 3.0) * 100.0
            else:
                s = -((iv - 16.0) / 8.0) * 100.0
            components["india_vix"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["india_vix"],
                "value": iv,
                "label": "INDIA_VIX",
            }

        # 1b. India VIX Velocity (Intraday / 1-Day change % - 12% weight)
        if getattr(indicators, "india_vix_change_pct", None) is not None:
            ivc = indicators.india_vix_change_pct
            if ivc >= 15.0:
                s = -100.0
            elif ivc <= -10.0:
                s = 100.0
            elif ivc > 0.0:
                s = -(ivc / 15.0) * 100.0
            elif ivc < 0.0:
                s = (abs(ivc) / 10.0) * 100.0
            else:
                s = 0.0
            components["india_vix_velocity"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["india_vix_velocity"],
                "value": ivc,
                "label": "INDIA_VIX_CHANGE_PCT (1D)",
            }

        # 2. VIX Volatility Shock (7-Day Rolling Move - 10% weight)
        if indicators.vix_change_pct is not None:
            vcp = indicators.vix_change_pct
            if vcp >= 15.0:
                s = -100.0
            elif vcp <= -10.0:
                s = 100.0
            elif vcp > 0.0:
                s = -(vcp / 15.0) * 100.0
            elif vcp < 0.0:
                s = (abs(vcp) / 10.0) * 100.0
            else:
                s = 0.0
            components["vix_shock"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["vix_shock"],
                "value": vcp,
                "label": "VIX_CHANGE_PCT (7D)",
            }

        # 3. Market Breadth AD Ratio (16% weight)
        if getattr(indicators, "ad_ratio", None) is not None:
            ad = indicators.ad_ratio
            if ad >= 2.0:
                s = 100.0
            elif ad <= 0.4:
                s = -100.0
            elif ad >= 1.0:
                s = (ad - 1.0) * 100.0
            else:
                s = -((1.0 - ad) / 0.6) * 100.0
            components["ad_ratio"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["ad_ratio"],
                "value": ad,
                "label": "AD_RATIO",
            }

        # 4. Nifty Stocks Above EMA20 % (12% weight) — de-saturated scale
        if getattr(indicators, "above_ema20_pct", None) is not None:
            ema = indicators.above_ema20_pct
            diff = ema - 50.0
            # Scale across a wider +-45% band so 80% gives +66.7 and 95% gives +100
            s = (diff / 45.0) * 100.0
            # Guard against breadth divergence saturation (Bug 6):
            # If advance/decline ratio is negative (< 1.0), cap bullish EMA score at +30.0
            ad = getattr(indicators, "ad_ratio", None)
            if ad is not None and ad < 1.0 and s > 0:
                s = min(s, 30.0)
            components["above_ema20"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["above_ema20"],
                "value": ema,
                "label": "ABOVE_EMA20_PCT",
            }

        # 5. Global US VIX (10% weight)
        if indicators.vix is not None:
            vix = indicators.vix
            if vix <= 14.0:
                s = 100.0
            elif vix >= 32.0:
                s = -100.0
            elif vix <= 18.0:
                s = ((18.0 - vix) / 4.0) * 100.0
            else:
                s = -((vix - 18.0) / 14.0) * 100.0
            components["us_vix"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["us_vix"],
                "value": vix,
                "label": "VIX",
            }

        # 6. Nifty Trend (10% weight)
        if indicators.nifty_trend:
            components["nifty_trend"] = {
                "score": self._trend_to_score(indicators.nifty_trend),
                "weight": self.WEIGHTS["nifty_trend"],
                "value": indicators.nifty_trend,
                "label": "NIFTY_TREND",
            }

        # 7. BTC Trend (5% weight)
        if indicators.btc_trend:
            components["btc_trend"] = {
                "score": self._trend_to_score(indicators.btc_trend),
                "weight": self.WEIGHTS["btc_trend"],
                "value": indicators.btc_trend,
                "label": "BTC_TREND",
            }

        # 8. USD/JPY Trend (3% weight - rising=carry on / risk-on)
        if indicators.usdjpy_trend:
            components["usdjpy_trend"] = {
                "score": self._trend_to_score(indicators.usdjpy_trend),
                "weight": self.WEIGHTS["usdjpy_trend"],
                "value": indicators.usdjpy_trend,
                "label": "USDJPY_TREND",
            }

        # 9. Gold Trend (2% weight - safe haven flight vs liquidation)
        if indicators.gold_trend:
            is_liquidation_regime = (
                (indicators.vix is not None and indicators.vix > self.VIX_HIGH_THRESHOLD)
                or (indicators.vix_change_pct is not None and abs(indicators.vix_change_pct) > 40.0)
                or (getattr(indicators, "india_vix", None) is not None and indicators.india_vix > self.INDIA_VIX_HIGH_THRESHOLD)
            )
            if is_liquidation_regime and indicators.gold_trend == "FALLING":
                s = -40.0
            else:
                s = -self._trend_to_score(indicators.gold_trend)
            components["gold_trend"] = {
                "score": max(-100.0, min(100.0, s)),
                "weight": self.WEIGHTS["gold_trend"],
                "value": indicators.gold_trend,
                "label": "GOLD_TREND",
            }

        return components

    def get_risk_breakdown(self, indicators: Optional[RiskIndicators] = None) -> dict:
        """
        Calculate full risk score breakdown with guaranteed arithmetic provenance.
        Returns:
            {
                "score": float,
                "raw_score": float,
                "published_sum": float,
                "weight_sum": float,
                "normalized": bool,
                "score_reconciles": bool,
                "adjustments": list[dict],
                "cautions": list[str],
                "details": list[dict],
            }
        """
        ind = indicators or self.fetch_risk_indicators()
        comp_scores = self.calculate_component_scores(ind)

        # Scored components
        scored_details = []
        for key in ["india_vix", "india_vix_velocity", "vix_shock", "ad_ratio", "above_ema20", "us_vix", "nifty_trend", "btc_trend", "usdjpy_trend", "gold_trend"]:
            if key in comp_scores:
                c = comp_scores[key]
                w = float(c["weight"])
                sc = round(float(c["score"]), 1)
                pts = round(sc * (w / 100.0), 3)
                scored_details.append({
                    "key": key,
                    "label": c["label"],
                    "value": c["value"],
                    "scored": True,
                    "weight_pct": round(w, 1),
                    "score": sc,
                    "points": pts,
                })

        # Context-only components
        vix_trend = "FLAT"
        if ind.vix_change_pct is not None:
            vix_trend = "RISING" if ind.vix_change_pct > 0 else ("FALLING" if ind.vix_change_pct < 0 else "FLAT")

        context_items = [
            ("vix_trend", "VIX_TREND", vix_trend),
            ("vix_1d_change_pct", "VIX_1D_CHANGE_PCT", getattr(ind, "vix_1d_change_pct", None)),
            ("india_vix_7d_change_pct", "INDIA_VIX_7D_CHANGE_PCT", getattr(ind, "india_vix_7d_change_pct", None)),
            ("india_vix_trend", "INDIA_VIX_TREND", getattr(ind, "india_vix_trend", "FLAT")),
            ("breadth_regime", "BREADTH_REGIME", getattr(ind, "breadth_regime", "NEUTRAL")),
            ("usdjpy_level", "USDJPY_LEVEL", ind.usdjpy_level),
        ]
        context_details = []
        for key, label, val in context_items:
            context_details.append({
                "key": key,
                "label": label,
                "value": val,
                "scored": False,
                "weight_pct": 0.0,
                "score": 0.0,
                "points": 0.0,
            })

        all_details = scored_details + context_details

        published_sum = sum(d["points"] for d in scored_details)
        weight_sum = sum(d["weight_pct"] for d in scored_details)

        if weight_sum > 0:
            raw_score = sum(d["score"] * d["weight_pct"] for d in scored_details) / weight_sum
        else:
            raw_score = 0.0

        adjustments: list[dict] = []
        # If weight_sum is not exactly 100, record weight normalization difference
        norm_diff = round(raw_score - published_sum, 2)
        if abs(weight_sum - 100.0) >= 0.1 and abs(norm_diff) >= 0.01:
            adjustments.append({
                "reason": "WEIGHT_NORMALIZATION",
                "weight_sum": round(weight_sum, 1),
                "points": norm_diff,
            })

        cautions = list(getattr(ind, "cautions", []) or [])
        caution_pts = 0.0
        # If cautions are active and posture reflects caution (e.g. SELECTIVE / OPEN_WITH_CAUTION)
        if cautions and getattr(ind, "posture", "") in ("OPEN_WITH_CAUTION", "SELECTIVE"):
            caution_pts = round(len(cautions) * -4.0, 1)
            adjustments.append({
                "reason": "CAUTION_PENALTY",
                "count": len(cautions),
                "points": caution_pts,
                "cautions": cautions,
            })

        # Calculate final reconciled score and explicitly itemize any bounds clamping
        unclamped = raw_score + caution_pts
        clamped = max(-100.0, min(100.0, unclamped))
        final_score = round(clamped, 1)
        if abs(clamped - unclamped) >= 0.01:
            pre_clamp_adj = sum(a.get("points", 0.0) for a in adjustments)
            clamp_pts = round(final_score - (published_sum + pre_clamp_adj), 2)
            adjustments.append({
                "reason": "SCORE_CLAMP",
                "unclamped": round(unclamped, 2),
                "points": clamp_pts,
            })

        total_adj = sum(a.get("points", 0.0) for a in adjustments)
        reconciles = abs((published_sum + total_adj) - final_score) < 0.05

        return {
            "score": final_score,
            "raw_score": round(raw_score, 1),
            "published_sum": round(published_sum, 2),
            "weight_sum": round(weight_sum, 1),
            "normalized": True,
            "score_reconciles": reconciles,
            "adjustments": adjustments,
            "cautions": cautions,
            "details": all_details,
        }

    def get_risk_score(self) -> float:
        """
        Calculate risk environment score using weighted graded saturation and explicit reconciliation.
        Returns:
            Risk score from -100 (extreme risk-off) to +100 (extreme risk-on)
        """
        breakdown = self.get_risk_breakdown()
        return breakdown["score"]

    def get_indicators_detail(self, indicators: Optional[RiskIndicators] = None) -> list[dict]:
        """
        Return comprehensive metadata breakdown for all 14 indicators.
        Separates actively scored metrics from context-only telemetry.
        """
        breakdown = self.get_risk_breakdown(indicators)
        return breakdown["details"]

    def fetch_risk_indicators(self) -> RiskIndicators:
        """
        Fetch latest risk indicators from free sources.

        Data sources:
        - VIX: Yahoo Finance (^VIX)
        - BTC: Yahoo Finance (BTC-USD) - fallback from Binance
        - Nifty: Yahoo Finance (^NSEI)
        - USD/JPY: Twelve Data API (free tier) or Yahoo Finance
        - Gold: Yahoo Finance (GC=F)

        Uses a per-cache-file single-flight lock so concurrent requests/threads
        never trigger a parallel Yahoo/RSS stampede.

        Returns:
            RiskIndicators dataclass
        """
        # Fast path before lock
        cached = self._load_cache()
        if cached and self._is_cache_fresh(cached):
            logger.info("Using fresh risk indicators from cache")
            return cached

        lock_key = str(self.cache_file.resolve()) if self.cache_file else "default"
        with _get_risk_fetch_lock(lock_key):
            # Re-check after acquiring single-flight lock in case another caller just refreshed
            cached = self._load_cache()
            if cached and self._is_cache_fresh(cached):
                logger.info("Using fresh risk indicators from cache (single-flight reuse)")
                return cached

            # Cache miss or stale — fetch from APIs
            logger.info("Fetching fresh risk indicators")

        # Fetch all indicators
            vix, vix_1d_change, vix_7d_change, vix_fetched_at = self._fetch_vix()
            india_vix, iv_1d_change, india_vix_trend, iv_7d_change, india_vix_fetched_at = self._fetch_india_vix()
            ad_ratio, above_ema20, breadth_regime = self._fetch_market_breadth()
            btc_trend = self._fetch_btc_trend()
            nifty_trend = self._fetch_nifty_trend()
            usdjpy_level, usdjpy_trend = self._fetch_usdjpy()
            gold_trend = self._fetch_gold_trend()

            now_utc = datetime.now(timezone.utc)
            cached_spike_time = getattr(cached, "spike_flag_time", None) if cached else None

            # Determine risk regime, vetoes, and posture
            risk_regime, vetoes, posture, data_integrity = self._evaluate_safety_gates(
                vix=vix,
                vix_change=vix_7d_change,
                vix_1d_change=vix_1d_change,
                btc_trend=btc_trend,
                nifty_trend=nifty_trend,
                usdjpy_trend=usdjpy_trend,
                gold_trend=gold_trend,
                india_vix=india_vix,
                india_vix_1d_change=iv_1d_change,
                ad_ratio=ad_ratio,
                vix_fetched_at=vix_fetched_at,
                spike_flag_time=cached_spike_time,
                now=now_utc,
            )

            spike_flag_time = cached_spike_time
            if data_integrity == "UNCONFIRMED_VOL_SPIKE" and not spike_flag_time:
                spike_flag_time = now_utc
            elif data_integrity in ("CONFIRMED", "VOL_SPIKE_EXPIRED", "VIX_SOURCE_STALE"):
                spike_flag_time = None

            cautions = self._derive_cautions(
                vix=vix,
                india_vix=india_vix,
                ad_ratio=ad_ratio,
                above_ema20=above_ema20,
                vetoes=vetoes,
            )
            if not vetoes and cautions and posture in ("OPEN", "SELECTIVE"):
                posture = "OPEN_WITH_CAUTION"

            indicators = RiskIndicators(
                vix=vix,
                vix_change_pct=vix_7d_change,
                vix_1d_change_pct=vix_1d_change,
                btc_trend=btc_trend,
                nifty_trend=nifty_trend,
                usdjpy_level=usdjpy_level,
                usdjpy_trend=usdjpy_trend,
                gold_trend=gold_trend,
                risk_regime=risk_regime,
                timestamp=now_utc,
                india_vix=india_vix,
                india_vix_change_pct=iv_1d_change,
                india_vix_7d_change_pct=iv_7d_change,
                india_vix_trend=india_vix_trend,
                ad_ratio=ad_ratio,
                above_ema20_pct=above_ema20,
                breadth_regime=breadth_regime,
                posture=posture,
                vetoes=vetoes,
                cautions=cautions,
                data_integrity=data_integrity,
                vix_fetched_at=vix_fetched_at,
                india_vix_fetched_at=india_vix_fetched_at,
                spike_flag_time=spike_flag_time,
            )

            # Save to cache
            self._save_cache(indicators)

            return indicators

    def calculate_trend(self, prices: list[float]) -> str:
        """
        Calculate trend from price series.

        Logic:
        - Current price > 7-day MA by >2% = "RISING"
        - Current price < 7-day MA by >2% = "FALLING"
        - Otherwise = "FLAT"

        Args:
            prices: List of prices (oldest to newest)

        Returns:
            Trend direction string: "RISING" | "FALLING" | "FLAT"
        """
        if len(prices) < self.TREND_MA_PERIOD:
            logger.warning(f"Insufficient data for trend calculation: {len(prices)} < {self.TREND_MA_PERIOD}")
            return "FLAT"

        # Calculate 7-day moving average
        ma_prices = prices[-self.TREND_MA_PERIOD:]
        ma = sum(ma_prices) / len(ma_prices)

        # Current price
        current = prices[-1]

        # Calculate percentage difference
        pct_diff = ((current - ma) / ma) * 100.0

        # Classify trend
        if pct_diff > self.TREND_THRESHOLD_PCT:
            return "RISING"
        elif pct_diff < -self.TREND_THRESHOLD_PCT:
            return "FALLING"
        else:
            return "FLAT"

    def _fetch_vix(self) -> tuple[Optional[float], Optional[float], Optional[float], Optional[datetime]]:
        """
        Fetch VIX index from Yahoo Finance.

        Returns:
            (current_vix, 1day_change_pct, 7day_change_pct, fetched_at) or (None, None, None, None)
        """
        try:
            ticker = yf.Ticker("^VIX")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No VIX data returned from Yahoo Finance")
                return None, None, None, None

            # Current VIX
            current_vix = float(hist["Close"].iloc[-1])

            # 1-day change
            vix_1d_change = None
            if len(hist) >= 2:
                vix_prev = float(hist["Close"].iloc[-2])
                if vix_prev > 0:
                    vix_1d_change = ((current_vix - vix_prev) / vix_prev) * 100.0

            # 7-day change
            if len(hist) >= 8:
                vix_7d_ago = float(hist["Close"].iloc[-8])
                vix_change = ((current_vix - vix_7d_ago) / vix_7d_ago) * 100.0
            else:
                vix_change = None

            fetched_at = datetime.now(timezone.utc)
            logger.info(
                f"VIX: {current_vix:.2f} (1d: {vix_1d_change:.1f}%, 7d: {vix_change:.1f}%)"
                if vix_1d_change is not None and vix_change is not None else f"VIX: {current_vix:.2f}"
            )
            return current_vix, vix_1d_change, vix_change, fetched_at

        except Exception as e:
            logger.error(f"Failed to fetch VIX: {e}")
            return None, None, None, None

    def _fetch_india_vix(self) -> tuple[Optional[float], Optional[float], Optional[str], Optional[float], Optional[datetime]]:
        """
        Fetch India VIX (^INDIAVIX) from Yahoo Finance.

        Returns:
            (current_india_vix, 1day_change_pct, trend, 7day_change_pct, fetched_at) or (None, None, None, None, None)
        """
        try:
            ticker = yf.Ticker("^INDIAVIX")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.debug("No India VIX data returned from Yahoo Finance")
                return None, None, None, None, None

            current_vix = float(hist["Close"].iloc[-1])

            # 1-day change
            iv_1d_change = None
            if len(hist) >= 2:
                iv_prev = float(hist["Close"].iloc[-2])
                if iv_prev > 0:
                    iv_1d_change = ((current_vix - iv_prev) / iv_prev) * 100.0

            if len(hist) >= 8:
                vix_7d_ago = float(hist["Close"].iloc[-8])
                vix_change = ((current_vix - vix_7d_ago) / vix_7d_ago) * 100.0
            else:
                vix_change = None

            prices = hist["Close"].tolist()
            trend = self.calculate_trend(prices)
            fetched_at = datetime.now(timezone.utc)
            logger.info(
                f"India VIX: {current_vix:.2f} (1d: {iv_1d_change:.1f}%, 7d: {vix_change:.1f}%, trend: {trend})"
                if iv_1d_change is not None and vix_change is not None else f"India VIX: {current_vix:.2f}"
            )
            return current_vix, iv_1d_change, trend, vix_change, fetched_at

        except Exception as e:
            logger.debug(f"Failed to fetch India VIX: {e}")
            return None, None, None, None, None

    def _fetch_market_breadth(self) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """
        Calculate Advance/Decline ratio and % above 20-period EMA across key Nifty constituent stocks.

        Returns:
            (ad_ratio, above_ema20_pct, breadth_regime)
        """
        try:
            import api.service as svc
            mdata = getattr(svc, "_market_data", {})
            symbols = [
                "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
                "SBIN", "BHARTIARTL", "ITC", "LT", "KOTAKBANK"
            ]
            advances = 0
            declines = 0
            above_ema = 0
            total_valid = 0

            for sym in symbols:
                df = mdata.get(sym)
                if df is None:
                    df = mdata.get(f"{sym}.NS")
                if df is not None and len(df) >= 20 and "close" in df.columns:
                    c = df["close"].values
                    if len(c) >= 2:
                        total_valid += 1
                        if c[-1] >= c[-2]:
                            advances += 1
                        else:
                            declines += 1
                        ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
                        if c[-1] >= ema20:
                            above_ema += 1

            if total_valid < 3:
                # Cold cache baseline
                return 1.2, 55.0, "NEUTRAL"

            ad_ratio = round(advances / max(declines, 1), 2)
            above_pct = round((above_ema / total_valid) * 100.0, 1)
            regime = "STRONG_BREADTH" if ad_ratio >= 1.5 and above_pct >= 60.0 else (
                "WEAK_BREADTH" if ad_ratio <= 0.7 or above_pct <= 40.0 else "NEUTRAL"
            )
            return ad_ratio, above_pct, regime
        except Exception as e:
            logger.debug(f"Market breadth calculation failed: {e}")
            return None, None, None

    def _fetch_btc_trend(self) -> Optional[str]:
        """
        Fetch BTC trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("BTC-USD")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No BTC data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"BTC trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch BTC trend: {e}")
            return None

    def _fetch_nifty_trend(self) -> Optional[str]:
        """
        Fetch Nifty 50 trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("^NSEI")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No Nifty data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"Nifty trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch Nifty trend: {e}")
            return None

    def _fetch_usdjpy(self) -> tuple[Optional[float], Optional[str]]:
        """
        Fetch USD/JPY rate and trend.

        Tries Twelve Data API first, falls back to Yahoo Finance.

        Returns:
            (current_rate, trend) or (None, None)
        """
        # Try Twelve Data API first (if key available)
        if TWELVE_DATA_API_KEY:
            try:
                # Fetch current rate
                url = "https://api.twelvedata.com/price"
                params = {
                    "symbol": "USD/JPY",
                    "apikey": TWELVE_DATA_API_KEY,
                }
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()

                if "price" in data:
                    current_rate = float(data["price"])

                    # Fetch historical data for trend
                    hist_url = "https://api.twelvedata.com/time_series"
                    hist_params = {
                        "symbol": "USD/JPY",
                        "interval": "1day",
                        "outputsize": 30,
                        "apikey": TWELVE_DATA_API_KEY,
                    }
                    hist_response = requests.get(hist_url, params=hist_params, timeout=10)
                    hist_response.raise_for_status()
                    hist_data = hist_response.json()

                    if "values" in hist_data:
                        prices = [float(v["close"]) for v in hist_data["values"]]
                        prices.reverse()  # Oldest to newest
                        trend = self.calculate_trend(prices)
                        logger.info(f"USD/JPY: {current_rate:.2f} (trend: {trend})")
                        return current_rate, trend

            except Exception as e:
                logger.warning(f"Twelve Data API failed for USD/JPY: {e}, falling back to Yahoo Finance")

        # Fallback to Yahoo Finance
        try:
            ticker = yf.Ticker("JPY=X")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No USD/JPY data returned from Yahoo Finance")
                return None, None

            # Current rate
            current_rate = float(hist["Close"].iloc[-1])

            # Extract closing prices for trend
            prices = hist["Close"].tolist()
            trend = self.calculate_trend(prices)

            logger.info(f"USD/JPY: {current_rate:.2f} (trend: {trend})")
            return current_rate, trend

        except Exception as e:
            logger.error(f"Failed to fetch USD/JPY: {e}")
            return None, None

    def _fetch_gold_trend(self) -> Optional[str]:
        """
        Fetch Gold trend from Yahoo Finance.

        Returns:
            "RISING" | "FALLING" | "FLAT" or None
        """
        try:
            ticker = yf.Ticker("GC=F")
            hist = ticker.history(period="1mo")

            if hist.empty:
                logger.error("No Gold data returned from Yahoo Finance")
                return None

            # Extract closing prices
            prices = hist["Close"].tolist()

            # Calculate trend
            trend = self.calculate_trend(prices)
            logger.info(f"Gold trend: {trend}")
            return trend

        except Exception as e:
            logger.error(f"Failed to fetch Gold trend: {e}")
            return None

    def _trend_to_score(self, trend: str) -> float:
        """
        Convert trend string to score.

        Args:
            trend: "RISING" | "FALLING" | "FLAT"

        Returns:
            Score: +100 (rising), -100 (falling), 0 (flat)
        """
        if trend == "RISING":
            return 100.0
        elif trend == "FALLING":
            return -100.0
        else:
            return 0.0

    def _calculate_risk_regime(
        self,
        vix: Optional[float],
        btc_trend: Optional[str],
        nifty_trend: Optional[str],
        usdjpy_trend: Optional[str],
        gold_trend: Optional[str],
        india_vix: Optional[float] = None,
        ad_ratio: Optional[float] = None,
    ) -> str:
        """
        Determine overall risk regime from indicators.

        Returns:
            "RISK_ON" | "RISK_OFF" | "NEUTRAL"
        """
        risk_on_signals = 0
        risk_off_signals = 0
        total_signals = 0

        # Global US VIX
        if vix is not None:
            total_signals += 1
            if vix < self.VIX_LOW_THRESHOLD:
                risk_on_signals += 1
            elif vix > self.VIX_HIGH_THRESHOLD:
                risk_off_signals += 1

        # Local India VIX (NSE fear index)
        if india_vix is not None:
            total_signals += 1
            if india_vix < self.INDIA_VIX_LOW_THRESHOLD:
                risk_on_signals += 1
            elif india_vix > self.INDIA_VIX_HIGH_THRESHOLD:
                risk_off_signals += 1

        # Market Breadth AD Ratio
        if ad_ratio is not None:
            total_signals += 1
            if ad_ratio >= 1.5:
                risk_on_signals += 1
            elif ad_ratio <= 0.7:
                risk_off_signals += 1

        # BTC
        if btc_trend:
            total_signals += 1
            if btc_trend == "RISING":
                risk_on_signals += 1
            elif btc_trend == "FALLING":
                risk_off_signals += 1

        # Nifty
        if nifty_trend:
            total_signals += 1
            if nifty_trend == "RISING":
                risk_on_signals += 1
            elif nifty_trend == "FALLING":
                risk_off_signals += 1

        # USD/JPY (Rising = carry trade active / risk-on; Falling = carry unwind / risk-off)
        if usdjpy_trend:
            total_signals += 1
            if usdjpy_trend == "RISING":
                risk_on_signals += 1
            elif usdjpy_trend == "FALLING":
                risk_off_signals += 1

        # Gold: Safe haven flight normally, but forced selling/liquidation in panic
        if gold_trend:
            total_signals += 1
            is_liquidation = (
                (vix is not None and vix > self.VIX_HIGH_THRESHOLD)
                or (india_vix is not None and india_vix > self.INDIA_VIX_HIGH_THRESHOLD)
            )
            if is_liquidation and gold_trend == "FALLING":
                risk_off_signals += 1
            else:
                if gold_trend == "FALLING":
                    risk_on_signals += 1
                elif gold_trend == "RISING":
                    risk_off_signals += 1

        # Determine regime based on majority
        if total_signals == 0:
            return "NEUTRAL"

        risk_on_pct = (risk_on_signals / total_signals) * 100.0
        risk_off_pct = (risk_off_signals / total_signals) * 100.0

        # Need >60% agreement for clear regime
        if risk_on_pct >= 60.0:
            return "RISK_ON"
        elif risk_off_pct >= 60.0:
            return "RISK_OFF"
        else:
            return "NEUTRAL"

    def _evaluate_safety_gates(
        self,
        vix: Optional[float],
        vix_change: Optional[float],
        btc_trend: Optional[str],
        nifty_trend: Optional[str],
        usdjpy_trend: Optional[str],
        gold_trend: Optional[str],
        india_vix: Optional[float] = None,
        india_vix_1d_change: Optional[float] = None,
        ad_ratio: Optional[float] = None,
        vix_1d_change: Optional[float] = None,
        vix_fetched_at: Optional[datetime] = None,
        spike_flag_time: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> tuple[str, list[str], str, str]:
        """
        Evaluate hard asymmetric risk vetoes and determine regime & posture.

        Danger is NOT democratic:
        - A single catastrophic reading (vol shock, severe breadth collapse, extreme VIX)
          trips a VETO.
        - Any active veto forces posture='STAND_DOWN' and regime='RISK_OFF'.
        - When no vetoes trip, regime follows consensus agreement.

        Returns:
            (regime, vetoes, posture, data_integrity)
        """
        vetoes = []
        data_integrity = "CONFIRMED"
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        # Provider read freshness check: stale US VIX (>15m) cannot veto
        is_vix_stale = False
        if vix_fetched_at is not None:
            v_fetch = vix_fetched_at
            if v_fetch.tzinfo is None:
                v_fetch = v_fetch.replace(tzinfo=timezone.utc)
            if (now_dt - v_fetch).total_seconds() > 15 * 60:
                is_vix_stale = True
                logger.warning(
                    f"US VIX fetch age is {(now_dt - v_fetch).total_seconds() / 60:.1f}m (>15m limit). Discarding US VIX vetoes."
                )
                vix = None
                vix_change = None
                vix_1d_change = None
                data_integrity = "VIX_SOURCE_STALE"

        # 1. Volatility shock check (distinguishing single-session panic shock from 7-day trend)
        is_vol_shock = False
        # Single-day panic move
        if vix_1d_change is not None and abs(vix_1d_change) > 40.0:
            is_vol_shock = True
        if india_vix_1d_change is not None and abs(india_vix_1d_change) > 30.0:
            is_vol_shock = True
        # Severe cumulative 7-day surge
        if vix_change is not None and abs(vix_change) > 75.0:
            is_vol_shock = True

        if (vix_1d_change is not None and abs(vix_1d_change) > 80.0) or (vix_change is not None and abs(vix_change) > 100.0):
            # Check if unconfirmed spike has persisted > 30 minutes
            if spike_flag_time is not None:
                sp_dt = spike_flag_time
                if sp_dt.tzinfo is None:
                    sp_dt = sp_dt.replace(tzinfo=timezone.utc)
                if (now_dt - sp_dt).total_seconds() > 30 * 60:
                    data_integrity = "VOL_SPIKE_EXPIRED"
                    is_vol_shock = False  # Expire unconfirmed spike veto
                else:
                    data_integrity = "UNCONFIRMED_VOL_SPIKE"
            else:
                data_integrity = "UNCONFIRMED_VOL_SPIKE"

        if is_vol_shock and not is_vix_stale:
            vetoes.append("VOL_SHOCK")

        # 2. Market breadth floor breakdown
        if ad_ratio is not None and ad_ratio < 0.35:
            vetoes.append("BREADTH_FLOOR")

        # 3. Local India VIX stress ceiling
        if india_vix is not None and india_vix > 21.0:
            vetoes.append("INDIA_VIX_CEILING")

        # 4. Global US VIX stress ceiling
        if vix is not None and vix > 32.0 and not is_vix_stale:
            vetoes.append("US_VIX_STRESS")

        # Consensus regime baseline
        consensus_regime = self._calculate_risk_regime(
            vix, btc_trend, nifty_trend, usdjpy_trend, gold_trend,
            india_vix=india_vix, ad_ratio=ad_ratio,
        )

        if vetoes:
            regime = "RISK_OFF"
            posture = "STAND_DOWN"
        else:
            regime = consensus_regime
            if regime == "RISK_OFF":
                posture = "STAND_DOWN"
            elif regime == "RISK_ON":
                posture = "OPEN"
            else:
                is_caution = (
                    (india_vix is not None and india_vix > 18.0)
                    or (vix is not None and vix > 25.0)
                    or (ad_ratio is not None and ad_ratio < 0.75)
                )
                posture = "OPEN_WITH_CAUTION" if is_caution else "OPEN"

        return regime, vetoes, posture, data_integrity

    def _derive_cautions(
        self,
        vix: Optional[float],
        india_vix: Optional[float],
        ad_ratio: Optional[float],
        above_ema20: Optional[float] = None,
        vetoes: Optional[list[str]] = None,
    ) -> list[str]:
        """Derive named market cautions for risk posture and execution sizing."""
        cautions = []
        if india_vix is not None and india_vix > 18.0:
            cautions.append("INDIA_VIX_ELEVATED")
        if vix is not None and vix > 25.0:
            cautions.append("US_VIX_ELEVATED")
        if ad_ratio is not None and ad_ratio < 0.75:
            cautions.append("BREADTH_WEAK")
        if above_ema20 is not None and above_ema20 < 40.0:
            cautions.append("MOMENTUM_DIVERGENCE")
        try:
            from agents.data_ingestion.context_fetcher import build_context_fetcher
            cf = build_context_fetcher()
            for sym in ("RELIANCE", "TCS", "INFY", "HDFCBANK"):
                snap = cf.get_snapshot_sync(sym)
                if snap and getattr(snap, "news_volume_spike", False):
                    cautions.append(f"NEWS_SPIKE_{sym}")
        except Exception:
            pass
        return cautions

    def _load_cache(self) -> Optional[RiskIndicators]:
        """Load risk indicators from process memory cache (when disk mtime unchanged) or JSON file."""
        cache_key = str(self.cache_file.resolve()) if self.cache_file else "default"
        try:
            disk_mtime = self.cache_file.stat().st_mtime if self.cache_file.exists() else None
        except OSError:
            disk_mtime = None

        with _EDGE_MEM_CACHE_LOCK:
            mem_entry = _EDGE_MEM_CACHE.get(cache_key)
            if mem_entry is not None and (disk_mtime is None or mem_entry[0] == disk_mtime):
                return mem_entry[1]

        if not self.cache_file.exists():
            return None

        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            indicators = RiskIndicators.from_dict(data)
            with _EDGE_MEM_CACHE_LOCK:
                _EDGE_MEM_CACHE[cache_key] = (disk_mtime or 0.0, indicators)
            return indicators
        except Exception as e:
            logger.error(f"Failed to load cache: {e}")
            return None

    def _save_cache(self, indicators: RiskIndicators) -> None:
        """Save risk indicators to cache file and process memory cache."""
        cache_key = str(self.cache_file.resolve()) if self.cache_file else "default"
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w") as f:
                json.dump(indicators.to_dict(), f, indent=2)
            try:
                disk_mtime = self.cache_file.stat().st_mtime
            except OSError:
                disk_mtime = 0.0
            with _EDGE_MEM_CACHE_LOCK:
                _EDGE_MEM_CACHE[cache_key] = (disk_mtime, indicators)
            logger.info(f"Saved risk indicators to cache: {self.cache_file}")
        except Exception as e:
            logger.error(f"Failed to save cache: {e}")

    def _is_cache_fresh(self, indicators: RiskIndicators) -> bool:
        """Check if cached indicators are still fresh"""
        ts = indicators.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()

        # In-session freshness contract: drop TTL to <= 60s when market is open
        effective_ttl = self.cache_ttl_seconds
        try:
            from agents.execution.session_guard import is_nse_market_open
            is_open_res = is_nse_market_open()
            is_open = bool(is_open_res[0]) if isinstance(is_open_res, tuple) else bool(is_open_res)
            if is_open:
                effective_ttl = min(self.cache_ttl_seconds, 60.0)
        except Exception:
            pass

        return age_seconds < effective_ttl
