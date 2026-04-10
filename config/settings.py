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

# Post-boost confidence: candle structure (indecision / micro-range / wrong-color bar)
CANDLE_QUALITY_CONF_ADJUST_ENABLED = os.getenv(
    "CANDLE_QUALITY_CONF_ADJUST_ENABLED", "true"
).lower() in ("1", "true", "yes")
CANDLE_QUALITY_MIN_BODY_RATIO = float(os.getenv("CANDLE_QUALITY_MIN_BODY_RATIO", "0.35"))
CANDLE_QUALITY_MIN_RANGE_ATR_RATIO = float(os.getenv("CANDLE_QUALITY_MIN_RANGE_ATR_RATIO", "0.25"))
CANDLE_QUALITY_BODY_PENALTY = float(os.getenv("CANDLE_QUALITY_BODY_PENALTY", "8.0"))
CANDLE_QUALITY_RANGE_PENALTY = float(os.getenv("CANDLE_QUALITY_RANGE_PENALTY", "5.0"))
CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY = float(os.getenv("CANDLE_QUALITY_DIRECTION_FIGHT_PENALTY", "6.0"))

# MTF agreement (HTF + EMA + MACD [+ VWAP] stack) when full mtf_score is not passed
MTF_VOTE_CONF_ADJUST_ENABLED = os.getenv("MTF_VOTE_CONF_ADJUST_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
MTF_AGREEMENT_MIN_VOTES = int(os.getenv("MTF_AGREEMENT_MIN_VOTES", "2"))
MTF_AGREEMENT_LOW_VOTE_PENALTY = float(os.getenv("MTF_AGREEMENT_LOW_VOTE_PENALTY", "7.0"))
# When caller passes mtf_score (-100..100), map to 0–4 display votes; penalize very weak alignment
MTF_SCORE_WEAK_THRESHOLD = float(os.getenv("MTF_SCORE_WEAK_THRESHOLD", "35"))
MTF_SCORE_WEAK_PENALTY = float(os.getenv("MTF_SCORE_WEAK_PENALTY", "6.0"))

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

# ── Context Layer (24hr + 7-day) ───────────────────────
# Default: no paid/keyed APIs — RSS, public Binance/CoinGecko, yfinance, NSE calendar HTTP.
# Set CONTEXT_USE_PAID_APIS=true only if you want MarketAux + RapidAPI calendar (keys required).
CONTEXT_USE_PAID_APIS = os.getenv("CONTEXT_USE_PAID_APIS", "false").lower() in ("1", "true", "yes")

# Try nsepython first for NSE day change in context layer (no key). Disable if NSE blocks your host.
NSEPYTHON_QUOTES_ENABLED = os.getenv("NSEPYTHON_QUOTES_ENABLED", "true").lower() in ("1", "true", "yes")

MARKETAUX_API_KEY = os.getenv("MARKETAUX_API_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

# Legacy keys kept for compatibility; free-only policy does not use paid endpoints.
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY", "")
COINMARKETCAP_API_KEY = os.getenv("COINMARKETCAP_API_KEY", "")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Context fetcher intervals (seconds)
CONTEXT_REFRESH_INTERVAL = int(os.getenv("CONTEXT_REFRESH_INTERVAL", "900"))
NEWS_REFRESH_INTERVAL = int(os.getenv("NEWS_REFRESH_INTERVAL", "1800"))
CALENDAR_REFRESH_INTERVAL = int(os.getenv("CALENDAR_REFRESH_INTERVAL", "14400"))

# Free-source cache/rate controls
COINGECKO_CACHE_MINUTES = int(os.getenv("COINGECKO_CACHE_MINUTES", "60"))
NEWS_CACHE_MINUTES = int(os.getenv("NEWS_CACHE_MINUTES", "240"))
CALENDAR_CACHE_HOURS = int(os.getenv("CALENDAR_CACHE_HOURS", "4"))
NSE_EVENTS_CACHE_HOURS = int(os.getenv("NSE_EVENTS_CACHE_HOURS", "24"))
MARKETAUX_DAILY_LIMIT = int(os.getenv("MARKETAUX_DAILY_LIMIT", "100"))
MARKETAUX_TOP_SYMBOLS = int(os.getenv("MARKETAUX_TOP_SYMBOLS", "10"))

# Context state files
CONTEXT_CACHE_FILE = os.getenv("CONTEXT_CACHE_FILE", "context_cache.json")
NEWS_CACHE_FILE = os.getenv("NEWS_CACHE_FILE", "news_cache.json")
DAILY_API_USAGE_FILE = os.getenv("DAILY_API_USAGE_FILE", "daily_api_usage.json")

# Context modifier thresholds
EVENT_RISK_DAYS_THRESHOLD = int(os.getenv("EVENT_RISK_DAYS_THRESHOLD", "2"))
NEWS_SPIKE_MULTIPLIER = float(os.getenv("NEWS_SPIKE_MULTIPLIER", "2.5"))
CONTEXT_STRONG_NEGATIVE = float(os.getenv("CONTEXT_STRONG_NEGATIVE", "-0.30"))
CONTEXT_STRONG_POSITIVE = float(os.getenv("CONTEXT_STRONG_POSITIVE", "0.40"))
EVENT_RISK_CONFIDENCE_PENALTY = float(os.getenv("EVENT_RISK_CONFIDENCE_PENALTY", "0.25"))

# Blend last-24h vs last-7d news narrative into context_score (sum ~0.30 by default).
CONTEXT_NEWS_WEIGHT_24HR = float(os.getenv("CONTEXT_NEWS_WEIGHT_24HR", "0.15"))
CONTEXT_NEWS_WEIGHT_7D = float(os.getenv("CONTEXT_NEWS_WEIGHT_7D", "0.15"))

# Ensemble dynamic weights
ENSEMBLE_WEIGHTS = {
    "TRENDING": {"xgboost": 0.40, "lightgbm": 0.35, "lstm": 0.25},
    "RANGING": {"xgboost": 0.30, "lightgbm": 0.25, "lstm": 0.45},
    "VOLATILE": {"xgboost": 0.50, "lightgbm": 0.35, "lstm": 0.15},
    "DEFAULT": {"xgboost": 0.35, "lightgbm": 0.35, "lstm": 0.30},
}

# Risk management and sizing
MAX_PORTFOLIO_HEAT_PCT = float(os.getenv("MAX_PORTFOLIO_HEAT_PCT", "0.06"))
CIRCUIT_BREAKER_THRESHOLD = float(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "0.12"))
FRACTIONAL_KELLY = float(os.getenv("FRACTIONAL_KELLY", "0.50"))
CORRELATION_GROUPS = [
    ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    ["RELIANCE.NS", "ONGC.NS"],
    ["HDFCBANK.NS", "ICICIBANK.NS", "KOTAKBANK.NS"],
]

# Feature drift controls
DRIFT_Z_THRESHOLD = float(os.getenv("DRIFT_Z_THRESHOLD", "3.0"))
DRIFT_CONFIDENCE_PENALTY = float(os.getenv("DRIFT_CONFIDENCE_PENALTY", "0.20"))

# Telegram deduplication
TELEGRAM_ALERT_COOLDOWN_HOURS = int(os.getenv("TELEGRAM_ALERT_COOLDOWN_HOURS", "4"))

# Backtest defaults
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.001"))
MIN_TRADES_FOR_RETRAIN = int(os.getenv("MIN_TRADES_FOR_RETRAIN", "200"))

# ── Swing Scanner Volume Thresholds ─────────────────────────────────────────
# Raised from 1.3x to eliminate false breakouts on mid/small-cap NSE stocks.
# Research: 1.5x-2x is the reliable threshold for sub-₹1000 Indian stocks.
SWING_VOL_RANGE_BREAKOUT = float(os.getenv("SWING_VOL_RANGE_BREAKOUT", "1.8"))   # 20-day range breakout
SWING_VOL_INSIDE_BAR     = float(os.getenv("SWING_VOL_INSIDE_BAR",     "2.0"))   # Inside bar breakout
SWING_VOL_BB_SQUEEZE     = float(os.getenv("SWING_VOL_BB_SQUEEZE",     "1.5"))   # BB squeeze expansion
SWING_VOL_EMA_PULLBACK   = float(os.getenv("SWING_VOL_EMA_PULLBACK",   "0.8"))   # EMA pullback (min interest)
SWING_VOL_52W_BREAKOUT   = float(os.getenv("SWING_VOL_52W_BREAKOUT",   "1.5"))   # 52-week high proximity
SWING_VOL_POST_RESULT    = float(os.getenv("SWING_VOL_POST_RESULT",    "3.0"))   # Post-result gap up

# Minimum average daily volume (shares) — below this = illiquid, penalise heavily.
SWING_MIN_AVG_VOL_SHARES = int(os.getenv("SWING_MIN_AVG_VOL_SHARES", "50000"))
SWING_LOW_LIQIDITY_PENALTY = int(os.getenv("SWING_LOW_LIQUIDITY_PENALTY", "20"))

# ── Swing Scanner SL Caps by Price Band ─────────────────────────────────────
# ATR-based SL for cheap stocks can be 5-8% which is too wide.
# Cap the SL at these levels; if exceeded, signal a "reduce qty" warning.
SWING_MAX_SL_PCT_BY_PRICE_BAND: dict[str, float] = {
    "under_200":   0.06,   # max 6% SL for stocks under ₹200
    "200_to_500":  0.05,   # max 5% SL
    "500_to_1000": 0.04,   # max 4% SL
}

# Minimum Risk:Reward for swing setups — lowered from 1.8 to 1.5 to allow more setups through.
SWING_MIN_RISK_REWARD = float(os.getenv("SWING_MIN_RISK_REWARD", "1.5"))

# Reference index for swing regime gate (must match NSE_SYMBOLS display name).
SWING_INDEX_SYMBOL = os.getenv("SWING_INDEX_SYMBOL", "NIFTY 50").strip() or "NIFTY 50"

# When True, drop LONG swing setups if index htf_trend is BEARISH (and optionally mirror for shorts).
SWING_REGIME_GATE_ENABLED = os.getenv("SWING_REGIME_GATE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
# When True, drop LONG swing setups if index htf_trend is BEARISH.
# DEFAULT: False — individual stocks can have valid LONG setups even in a bearish index.
# Individual pattern R:R + strength gates already protect against weak setups.
SWING_BLOCK_LONG_IN_BEARISH_TREND = os.getenv(
    "SWING_BLOCK_LONG_IN_BEARISH_TREND", "false"
).lower() in ("1", "true", "yes")
SWING_BLOCK_SHORT_IN_BULLISH_TREND = os.getenv(
    "SWING_BLOCK_SHORT_IN_BULLISH_TREND", "false"
).lower() in ("1", "true", "yes")

# Skip scanning a symbol if the last daily bar is older than N calendar days (0 = off).
SWING_MAX_BAR_AGE_DAYS = float(os.getenv("SWING_MAX_BAR_AGE_DAYS", "10"))

# Number of swing setups returned by API cache (higher = fewer names get hidden).
SWING_MAX_SETUPS_RETURN = int(os.getenv("SWING_MAX_SETUPS_RETURN", "100"))

# Keep only the single strongest setup per symbol when multiple patterns fire.
SWING_DEDUPE_ONE_PER_SYMBOL = os.getenv("SWING_DEDUPE_ONE_PER_SYMBOL", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Multi-pattern confluence: when several patterns agree (same direction), merge into one row
# with a strength bonus (capped). Conflicting LONG vs SHORT resolved by peak strength, then count.
SWING_CONFLUENCE_ENABLED = os.getenv("SWING_CONFLUENCE_ENABLED", "true").lower() in ("1", "true", "yes")
SWING_CONFLUENCE_BONUS_PER_EXTRA = int(os.getenv("SWING_CONFLUENCE_BONUS_PER_EXTRA", "5"))
SWING_CONFLUENCE_MAX_BONUS = int(os.getenv("SWING_CONFLUENCE_MAX_BONUS", "15"))

# ── NSE Sector Map ──────────────────────────────────────────────────────────
# Static mapping of tracked symbols → sector for sector momentum calculation.
# Key = display name used in NSE_SYMBOLS, Value = sector bucket.
NSE_SECTOR_MAP: dict[str, str] = {
    # Indices (excluded from sector momentum)
    "NIFTY 50":     "_INDEX",
    "BANKNIFTY":    "_INDEX",
    "SENSEX":       "_INDEX",
    "NIFTY IT":     "_INDEX",
    "NIFTY BANK":   "_INDEX",
    # Banking & Financials
    "HDFCBANK":     "Banking",
    "ICICIBANK":    "Banking",
    "KOTAKBANK":    "Banking",
    "SBIN":         "Banking",
    "AXISBANK":     "Banking",
    "BAJFINANCE":   "Banking",
    # IT / Technology
    "TCS":          "IT",
    "INFY":         "IT",
    "WIPRO":        "IT",
    "HCLTECH":      "IT",
    "TECHM":        "IT",
    "LTIM":         "IT",
    # Oil & Gas / Energy
    "RELIANCE":     "Energy",
    "ONGC":         "Energy",
    "BPCL":         "Energy",
    "IOC":          "Energy",
    # Auto & Auto Ancillary
    "MARUTI":       "Auto",
    "TATAMOTORS":   "Auto",
    "BAJAJ-AUTO":   "Auto",
    "EICHERMOT":    "Auto",
    "HEROMOTOCO":   "Auto",
    # Pharma & Healthcare
    "SUNPHARMA":    "Pharma",
    "DRREDDY":      "Pharma",
    "CIPLA":        "Pharma",
    "DIVISLAB":     "Pharma",
    # FMCG / Consumer
    "HINDUNILVR":   "FMCG",
    "ITC":          "FMCG",
    "NESTLEIND":    "FMCG",
    "TITAN":        "FMCG",
    # Infrastructure & Capital Goods
    "LT":           "Infra",
    "ADANIENT":     "Infra",
    "ADANIPORTS":   "Infra",
    "POWERGRID":    "Infra",
    "NTPC":         "Infra",
    "BHARTIARTL":   "Telecom",
    # Metals & Mining
    "TATASTEEL":    "Metals",
    "JSWSTEEL":     "Metals",
    "HINDALCO":     "Metals",
    "SAIL":         "Metals",
    "NATIONALUM":   "Metals",
    "NMDC":         "Metals",
    "GMDCLTD":      "Metals",
    "JINDALSAW":    "Metals",
    "JINDALSTEL":   "Metals",
    "VEDL":         "Metals",
    # Defence & PSU
    "HAL":          "Defence",
    "BEL":          "Defence",
    "MAZDOCK":      "Defence",
    "COCHINSHIP":   "Defence",
    "RITES":        "Infra",
    "RAILVIKAS":    "Infra",
    "IRFC":         "Finance",
    "HGINFRA":      "Infra",
    "KALPATPOWR":   "Infra",
    # Pharma Small/Mid
    "CAPLIPOINT":   "Pharma",
    "APOLLOHOSP":   "Pharma",
    "CUPID":        "Pharma",
    # Industrials / Engineering
    "TIINDIA":      "Capital Goods",
    "GRINDWELL":    "Capital Goods",
    "ELECON":       "Capital Goods",
    "DYNAMATECH":   "Capital Goods",
    "SIEMENS":      "Capital Goods",
    "ABB":          "Capital Goods",
    "BHEL":         "Capital Goods",
    "POLYCAB":      "Capital Goods",
    "HAVELLS":      "Capital Goods",
    "VOLTAS":       "Capital Goods",
    # Wires & Cables
    "PRECWIRE":     "Capital Goods",
    "MOLDTKPAC":    "Consumer",
    # Consumer / Discretionary
    "TITAN":        "Consumer",
    "KALYANKJIL":   "Consumer",
    "DMART":        "Consumer",
    "TRENT":        "Consumer",
    "DIXON":        "Consumer",
    "OLAELEC":      "Auto",
    # Realty
    "DLF":          "Realty",
    "GODREJPROP":   "Realty",
    "PRESTIGE":     "Realty",
    # New Economy
    "IRCTC":        "Consumer",
    "ZOMATO":       "Consumer",
    "TATAPOWER":    "Energy",
    "COALINDIA":    "Energy",
    "GAIL":         "Energy",
}

# ── Swing Trading Watchlist ──────────────────────────────────────────────────
# Curated list of symbols the SwingSpectrum scanner should focus on.
# Mix of: user's preferred small/mid-caps + liquid large-caps for context.
# Order matters: first 20 are priority-loaded in Phase 3 of initial load.
SWING_WATCHLIST: list[str] = [
    # ─ User's target stocks ────────────────────────────
    "CUPID",        # Small-cap — high % moves, great swing candidate
    "PRECWIRE",     # Small-cap wires — volatile, good R:R setups
    "TATASTEEL",    # Large-cap metals — strong trend/momentum
    "ITC",          # Large-cap FMCG — reliable support/resistance

    # ─ Metals & Mining (high-beta swing plays) ─────────
    "JSWSTEEL",
    "HINDALCO",
    "SAIL",
    "NATIONALUM",
    "NMDC",
    "JINDALSAW",
    "VEDL",
    "COALINDIA",

    # ─ Defence & PSU (strong chart patterns) ───────────
    "HAL",
    "BEL",
    "MAZDOCK",
    "COCHINSHIP",
    "RAILVIKAS",
    "IRFC",
    "RITES",
    "BHEL",

    # ─ Capital Goods / Engineering ─────────────────────
    "TIINDIA",
    "GRINDWELL",
    "ELECON",
    "SIEMENS",
    "ABB",
    "POLYCAB",
    "HAVELLS",
    "HGINFRA",

    # ─ Pharma Mid-Cap ──────────────────────────────────
    "CAPLIPOINT",
    "SUNPHARMA",
    "CIPLA",

    # ─ Consumer / Discretionary ─────────────────────────
    "TRENT",
    "DIXON",
    "OLAELEC",
    "IRCTC",
    "KALYANKJIL",

    # ─ Large-cap for market context ────────────────────
    "RELIANCE",
    "SBIN",
    "TATAMOTORS",
    "ADANIPORTS",
    "TATAPOWER",
]
