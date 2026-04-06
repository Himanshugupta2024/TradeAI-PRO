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
AI_CONFIDENCE_THRESHOLD = 50  # percent — raised from 25. Below 50% = near coin-toss, don't trade.
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

# Regime-adaptive RSI thresholds
RSI_OVERSOLD_RANGING = 25   # Stricter in ranging markets
RSI_OVERBOUGHT_RANGING = 75
ATR_SL_MULTIPLIER_HIGHVOL = 2.0  # Wider SL in high volatility

# ── Adaptive per-regime quality & confidence thresholds ─────────────────────
# Quality score (0-100) minimum to emit LONG/SHORT (below → SKIP)
REGIME_QUALITY_THRESHOLDS: dict[str, int] = {
    "TRENDING":        60,   # Restored — momentum trades need solid confirmation
    "RANGING":         65,   # Restored — ranging market signals must be high quality
    "HIGH_VOLATILITY": 62,   # Restored — noise is high, only take clean setups
}

# AI confidence minimum per regime (overrides global AI_CONFIDENCE_THRESHOLD)
REGIME_CONFIDENCE_THRESHOLDS: dict[str, int] = {
    "TRENDING":        50,   # Raised from 20 — even in strong trends, need 50%+ confidence
    "RANGING":         55,   # Raised from 25 — ranging markets are tricky, need more conviction
    "HIGH_VOLATILITY": 52,   # Raised from 22 — high vol requires confident signals only
}

# Weighted Signal Scoring thresholds (raised to require stronger confirmation)
SIGNAL_SCORE_THRESHOLDS: dict[str, int] = {
    "TRENDING":        32,   # Raised from 28 — HTF(12) + RSI/EMA trigger + some confirmation
    "RANGING":         35,   # Raised from 30 — need multiple active triggers in ranging market
    "HIGH_VOLATILITY": 33,   # Raised from 30 — volatile markets, require solid setup
}
SIGNAL_SCORE_DEFAULT = 33   # Raised from 30 — requires real directional conviction

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
