"""
Central configuration for the AI Trading System.
Loaded from environment variables with sensible defaults.
"""

import os
from dotenv import load_dotenv

load_dotenv()


# ── Database ─────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trading.db")

# ── Symbols ──────────────────────────────────────────────
# Configurable via env vars (comma-separated) or defaults
_crypto_env = os.getenv("CRYPTO_SYMBOLS", "")
CRYPTO_SYMBOLS = [s.strip() for s in _crypto_env.split(",") if s.strip()] if _crypto_env else [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"
]

_nse_env = os.getenv("NSE_SYMBOLS", "")
NSE_SYMBOLS = [s.strip() for s in _nse_env.split(",") if s.strip()] if _nse_env else [
    "NIFTY 50", "BANKNIFTY", "RELIANCE", "TCS",
    "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
]

# yfinance ticker mapping for NSE
NSE_TICKER_MAP = {
    "NIFTY 50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "SBIN": "SBIN.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "LT": "LT.NS",
    "ITC": "ITC.NS",
    "AXISBANK": "AXISBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "BAJFINANCE": "BAJFINANCE.NS",
    "MARUTI": "MARUTI.NS",
    "TITAN": "TITAN.NS",
    "WIPRO": "WIPRO.NS",
    "TATAMOTORS": "TATAMOTORS.NS",
    "SUNPHARMA": "SUNPHARMA.NS",
    "ADANIENT": "ADANIENT.NS",
}

# ── Timeframes ───────────────────────────────────────────
TIMEFRAMES = ["5m", "15m", "1h", "1d"]

# Higher timeframe used for trend confirmation
HTF_TREND_TIMEFRAME = "1h"

# Multi-timeframe weight distribution (must sum to 1.0)
# Intraday-optimized: 15m + 1h get more weight for faster signal response
MTF_WEIGHTS = {"1d": 0.20, "1h": 0.35, "15m": 0.35, "5m": 0.10}

# ── Indicator Parameters ─────────────────────────────────
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
EMA_PERIODS = [20, 50, 200]
ATR_PERIOD = 14

# ADX (Trend Strength)
ADX_PERIOD = 14

# Bollinger Bands (Regime Detection)
BB_PERIOD = 20
BB_STD = 2.0

# ── Risk Management ─────────────────────────────────────
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
MIN_RISK_REWARD = float(os.getenv("MIN_RISK_REWARD", "2.0"))  # Minimum 1:2 Risk:Reward

# ── AI / Signal Filtering ────────────────────────────────
# Minimum ATR as % of price to avoid low-volatility noise signals
MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.15"))  # Reduced from 0.3 — less aggressive noise filter

# Lookback period for RSI divergence detection
DIVERGENCE_LOOKBACK = int(os.getenv("DIVERGENCE_LOOKBACK", "14"))

# Stochastic RSI thresholds
STOCHRSI_OVERSOLD = float(os.getenv("STOCHRSI_OVERSOLD", "20"))
STOCHRSI_OVERBOUGHT = float(os.getenv("STOCHRSI_OVERBOUGHT", "80"))

# Money Flow Index thresholds
MFI_OVERSOLD = float(os.getenv("MFI_OVERSOLD", "20"))
MFI_OVERBOUGHT = float(os.getenv("MFI_OVERBOUGHT", "80"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))
ATR_SL_MULTIPLIER = 2.0  # SL = Entry ± (2.0 * ATR) — wider to avoid fake stop-outs
RISK_PER_TRADE_PCT = float(os.getenv("RISK_PER_TRADE_PCT", "2.0"))

# Trailing Stop-Loss
TRAILING_ATR_MULTIPLIER = float(os.getenv("TRAILING_ATR_MULTIPLIER", "2.0"))
BREAKEVEN_ATR_THRESHOLD = float(os.getenv("BREAKEVEN_ATR_THRESHOLD", "1.0"))

# Cooldown: prevent re-entry into same symbol for N candles after exit
COOLDOWN_CANDLES = int(os.getenv("COOLDOWN_CANDLES", "3"))

# ── Signal Thresholds ────────────────────────────────────
AI_CONFIDENCE_THRESHOLD = 40  # Lowered from 50 — 40% is the new SKIP threshold

# ── Volume Spike Confirmation ─────────────────────────────────────────────────
# Signal is "volume-confirmed" when current volume > this × 20-bar avg.
# Volume-confirmed signals get bonus score + confidence.
VOLUME_SPIKE_MULTIPLIER  = float(os.getenv("VOLUME_SPIKE_MULTIPLIER",  "1.5"))  # 1.5x avg
VOLUME_SPIKE_SCORE_BONUS = int(os.getenv("VOLUME_SPIKE_SCORE_BONUS",   "15"))   # +15 pts buy/sell score
VOLUME_SPIKE_CONF_BONUS  = int(os.getenv("VOLUME_SPIKE_CONF_BONUS",    "20"))   # +20 pts confidence

# ── Multi-Timeframe Alignment Bonus ───────────────────────────────────────────
# When 15m + 1h + 1d all agree on direction → add to confidence.
MTF_ALIGNMENT_CONF_BONUS = int(os.getenv("MTF_ALIGNMENT_CONF_BONUS", "15"))  # +15 pts

# ── RSI Divergence Confidence Boost ───────────────────────────────────────────
# Bullish/bearish RSI divergence → add to AI confidence (on top of score logic).
RSI_DIVERGENCE_CONF_BONUS = int(os.getenv("RSI_DIVERGENCE_CONF_BONUS", "15"))  # +15 pts

# ── S/R Level Test Confidence Bonus ───────────────────────────────────────────
# Price testing key S/R level (within 0.5% of support/resistance) → +10 conf pts.
SR_TEST_CONF_BONUS = int(os.getenv("SR_TEST_CONF_BONUS", "10"))  # +10 pts

# ── SKIP Confidence Threshold ─────────────────────────────────────────────────
# Signals with AI confidence below this → SKIP (not LONG/SHORT).
# Lowered from 55 → 40: 40-55% confidence setups are valid in trending markets.
SKIP_CONFIDENCE_THRESHOLD = int(os.getenv("SKIP_CONFIDENCE_THRESHOLD", "40"))

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# Regime-adaptive RSI thresholds
RSI_OVERSOLD_RANGING = 25   # Stricter in ranging markets
RSI_OVERBOUGHT_RANGING = 75
ATR_SL_MULTIPLIER_HIGHVOL = 2.0  # Wider SL in high volatility

# ── Adaptive per-regime quality & confidence thresholds ──────────────────────
# Quality score (0-100) minimum to emit LONG/SHORT (below → SKIP).
# Lowered from 60-65 to reduce the double-gatekeeping that caused 34-46% stagnation.
REGIME_QUALITY_THRESHOLDS: dict[str, int] = {
    "TRENDING":        52,   # Lowered from 60: trending setups need less confirmation
    "RANGING":         58,   # Lowered from 65: ranging needs quality but not perfection
    "HIGH_VOLATILITY": 55,   # Lowered from 62: vol setups often look "noisy" but valid
}

# AI confidence minimum per regime.
# Key fix: lowered from 50-55 → 38-45. The fallback composite realistically peaks at
# ~54 in normal markets, so 50-55 was blocking virtually every signal.
REGIME_CONFIDENCE_THRESHOLDS: dict[str, int] = {
    "TRENDING":        42,   # Lowered from 50: strong trend = directional clarity
    "RANGING":         45,   # Lowered from 55: ranging still needs more conviction
    "HIGH_VOLATILITY": 40,   # Lowered from 52: volatile = allow lower conf, quality gates it
}

# Weighted Signal Scoring thresholds (buy_score/sell_score must exceed this).
# Score thresholds kept near previous — score is not the bottleneck, confidence is.
SIGNAL_SCORE_THRESHOLDS: dict[str, int] = {
    "TRENDING":        30,   # Slightly lower: trending market needs fewer triggers
    "RANGING":         35,   # Keep: ranging needs multiple active triggers
    "HIGH_VOLATILITY": 32,   # Slightly lower: vol spikes add lots of noise
}
SIGNAL_SCORE_DEFAULT = 32   # Real conviction still required

# Outcome label parameters
OUTCOME_FORWARD_BARS = int(os.getenv("OUTCOME_FORWARD_BARS", "5"))   # candles to look forward
OUTCOME_RR_RATIO     = float(os.getenv("OUTCOME_RR_RATIO", "2.0"))   # TP = entry ± RR × SL_dist

# ── Data Fetch Defaults ──────────────────────────────────
HISTORICAL_DAYS = 730  # ~2 years
BINANCE_RATE_LIMIT = True

# ── Scheduling ───────────────────────────────────────────
NSE_MARKET_CLOSE_IST = "15:30"  # 3:30 PM IST
DAILY_REPORT_ENABLED = os.getenv("DAILY_REPORT_ENABLED", "true").lower() == "true"
WEEKLY_REPORT_DAY = "friday"
HEALTH_FILE = os.getenv("HEALTH_FILE", "health.json")

# ── WebSocket Settings ───────────────────────────────────
WS_ENABLED = os.getenv("WS_ENABLED", "true").lower() == "true"
WS_RECONNECT_MAX = int(os.getenv("WS_RECONNECT_MAX", "50"))
WS_STALE_THRESHOLD_MULT = float(os.getenv("WS_STALE_THRESHOLD_MULT", "2.0"))
# If WebSocket is unavailable for this many seconds, fall back to CCXT polling
WS_FALLBACK_TIMEOUT = int(os.getenv("WS_FALLBACK_TIMEOUT", "30"))
