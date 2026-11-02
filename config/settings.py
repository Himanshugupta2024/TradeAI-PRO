"""
Central configuration for the AI Trading System.
Loaded from environment variables with sensible defaults.
"""

import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


# ── Environment & Security ────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT in {"production", "prod"}
ON_RENDER = bool(os.getenv("RENDER"))
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()

# ── Database ─────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./trading.db")

# ── Symbols ──────────────────────────────────────────────
# Configurable via env vars (comma-separated) or defaults
_crypto_env = os.getenv("CRYPTO_SYMBOLS", "")
CRYPTO_SYMBOLS = [s.strip() for s in _crypto_env.split(",") if s.strip()] if _crypto_env else [
    # ── Major Coins ────────────────────────────────────────
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    # ── OG Meme Coins ──────────────────────────────────────
    "DOGE/USDT", "SHIB/USDT",
    # ── Solana Meme Coins ───────────────────────────────────
    "BONK/USDT", "WIF/USDT", "BOME/USDT", "MEW/USDT", "POPCAT/USDT",
    # ── Ethereum/Base Meme Coins ────────────────────────────
    "PEPE/USDT", "FLOKI/USDT", "BRETT/USDT", "TURBO/USDT", "NEIRO/USDT",
    # ── AI-themed Meme Coins ────────────────────────────────
    "GOAT/USDT", "CHILLGUY/USDT",
    # ── Farcaster / Base Ecosystem ──────────────────────────
    "DEGEN/USDT",
    # ── Viral / Political Meme Coins ────────────────────────
    "PNUT/USDT", "TRUMP/USDT", "FARTCOIN/USDT", "MOODENG/USDT",
    # ── Newer Meme Gems ─────────────────────────────────────
    "SPX/USDT", "GIGA/USDT", "PONKE/USDT",
    # ── 2025 Trending Meme Coins ────────────────────────────
    "MICHI/USDT",    # Solana cat meme
    "MYRO/USDT",     # Solana dog meme
    "SLERF/USDT",    # Solana sloth meme
    "SILLY/USDT",    # Solana dragon meme
    "MUMU/USDT",     # Bull meme
    "DOGS/USDT",     # Telegram dog meme
    "HMSTR/USDT",    # Hamster Kombat
    "BABYDOGE/USDT", # Baby Doge
    "LADYS/USDT",    # Milady meme
    "WOJAK/USDT",    # Wojak meme
]

# Tags for meme coins — used by API to filter and badge them in the dashboard
MEME_COIN_SYMBOLS = [
    "DOGE/USDT", "SHIB/USDT", "BONK/USDT", "WIF/USDT", "BOME/USDT",
    "MEW/USDT", "POPCAT/USDT", "PEPE/USDT", "FLOKI/USDT", "BRETT/USDT",
    "TURBO/USDT", "NEIRO/USDT", "GOAT/USDT", "CHILLGUY/USDT", "DEGEN/USDT",
    "PNUT/USDT", "TRUMP/USDT", "FARTCOIN/USDT", "MOODENG/USDT",
    "SPX/USDT", "GIGA/USDT", "PONKE/USDT",
    # 2025 additions
    "MICHI/USDT", "MYRO/USDT", "SLERF/USDT", "SILLY/USDT", "MUMU/USDT",
    "DOGS/USDT", "HMSTR/USDT", "BABYDOGE/USDT", "LADYS/USDT", "WOJAK/USDT",
]

# Dynamic meme universe expansion (Binance public ticker scan)
MEME_DYNAMIC_DISCOVERY_ENABLED = os.getenv("MEME_DYNAMIC_DISCOVERY_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
MEME_DYNAMIC_DISCOVERY_LIMIT = int(os.getenv("MEME_DYNAMIC_DISCOVERY_LIMIT", "20"))
MEME_DYNAMIC_MIN_QUOTE_VOLUME_USDT = float(
    os.getenv("MEME_DYNAMIC_MIN_QUOTE_VOLUME_USDT", "5000000")
)
MEME_MAX_TRACKED_COINS = int(os.getenv("MEME_MAX_TRACKED_COINS", "60"))


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

# Intraday scanner: skip symbols when latest 5m bar is older than this threshold.
# Set 0 to disable freshness gating.
INTRADAY_MAX_BAR_AGE_MINUTES = int(os.getenv("INTRADAY_MAX_BAR_AGE_MINUTES", "120"))

# Intraday risk controls (paper-first defaults).
# These values shape signal sizing/filters for intraday scanner output.
INTRADAY_VIRTUAL_CAPITAL = float(os.getenv("INTRADAY_VIRTUAL_CAPITAL", "100000"))
INTRADAY_RISK_PER_TRADE_PCT = float(os.getenv("INTRADAY_RISK_PER_TRADE_PCT", "1.0"))
INTRADAY_ATR_SL_MULTIPLIER = float(os.getenv("INTRADAY_ATR_SL_MULTIPLIER", "1.5"))
INTRADAY_TARGET_ATR_MULTIPLIER = float(os.getenv("INTRADAY_TARGET_ATR_MULTIPLIER", "3.0"))
INTRADAY_MIN_RISK_REWARD = float(os.getenv("INTRADAY_MIN_RISK_REWARD", "1.5"))
INTRADAY_MAX_OPEN_TRADES = int(os.getenv("INTRADAY_MAX_OPEN_TRADES", "4"))
INTRADAY_MAX_DAILY_LOSS_PCT = float(os.getenv("INTRADAY_MAX_DAILY_LOSS_PCT", "0.02"))
INTRADAY_MAX_SINGLE_TRADE_PCT = float(os.getenv("INTRADAY_MAX_SINGLE_TRADE_PCT", "0.20"))
INTRADAY_SQUARE_OFF_HOUR_IST = int(os.getenv("INTRADAY_SQUARE_OFF_HOUR_IST", "15"))
INTRADAY_SQUARE_OFF_MINUTE_IST = int(os.getenv("INTRADAY_SQUARE_OFF_MINUTE_IST", "15"))
INTRADAY_CONFLUENCE_ENABLED = os.getenv("INTRADAY_CONFLUENCE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
INTRADAY_CONFLUENCE_MIN_AGREE = int(os.getenv("INTRADAY_CONFLUENCE_MIN_AGREE", "2"))
INTRADAY_CONFLUENCE_SCORE_BONUS_PER_EXTRA = float(
    os.getenv("INTRADAY_CONFLUENCE_SCORE_BONUS_PER_EXTRA", "4.0")
)
INTRADAY_VWAP_BAND_SIGMA = float(os.getenv("INTRADAY_VWAP_BAND_SIGMA", "1.0"))
INTRADAY_SUPERTREND_BASE_MULTIPLIER = float(os.getenv("INTRADAY_SUPERTREND_BASE_MULTIPLIER", "2.5"))
INTRADAY_SUPERTREND_LOWVOL_MULTIPLIER = float(os.getenv("INTRADAY_SUPERTREND_LOWVOL_MULTIPLIER", "2.0"))
INTRADAY_SUPERTREND_HIGHVOL_MULTIPLIER = float(os.getenv("INTRADAY_SUPERTREND_HIGHVOL_MULTIPLIER", "3.0"))
INTRADAY_SUPERTREND_LOWVOL_ATR_PCT = float(os.getenv("INTRADAY_SUPERTREND_LOWVOL_ATR_PCT", "0.6"))
INTRADAY_SUPERTREND_HIGHVOL_ATR_PCT = float(os.getenv("INTRADAY_SUPERTREND_HIGHVOL_ATR_PCT", "1.5"))

# Intraday Live Feed, RVOL, and Index Confluence settings
INTRADAY_LIVE_FEED_ENABLED = os.getenv("INTRADAY_LIVE_FEED_ENABLED", "true").lower() in ("true", "1", "yes")
INTRADAY_MIN_RVOL = float(os.getenv("INTRADAY_MIN_RVOL", "1.5"))
INTRADAY_RVOL_GATE_ENABLED = os.getenv("INTRADAY_RVOL_GATE_ENABLED", "true").lower() in ("true", "1", "yes")
INTRADAY_INDEX_CONFLUENCE_GATE = os.getenv("INTRADAY_INDEX_CONFLUENCE_GATE", "true").lower() in ("true", "1", "yes")
INTRADAY_ENTRY_CUTOFF_HOUR_IST = int(os.getenv("INTRADAY_ENTRY_CUTOFF_HOUR_IST", "14"))
INTRADAY_ENTRY_CUTOFF_MINUTE_IST = int(os.getenv("INTRADAY_ENTRY_CUTOFF_MINUTE_IST", "30"))


# NSE fetch timeout per symbol (seconds) for startup/refresh concurrency workers.
NSE_SYMBOL_TIMEOUT_SECONDS = float(os.getenv("NSE_SYMBOL_TIMEOUT_SECONDS", "150"))

# Background refresh controls.
REFRESH_INTERVAL_SEC = int(os.getenv("REFRESH_INTERVAL_SEC", "60"))
REFRESH_CYCLE_TIMEOUT_SEC = int(os.getenv("REFRESH_CYCLE_TIMEOUT_SEC", "120"))
BACKGROUND_READY_WAIT_SEC = int(os.getenv("BACKGROUND_READY_WAIT_SEC", "180"))

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

# Partial take-profit (execution-level)
PARTIAL_TP_ENABLED = os.getenv("PARTIAL_TP_ENABLED", "true").lower() in ("1", "true", "yes")
PARTIAL_TP_CLOSE_FRACTION = float(os.getenv("PARTIAL_TP_CLOSE_FRACTION", "0.5"))
PARTIAL_TP_TRIGGER_FRACTION = float(os.getenv("PARTIAL_TP_TRIGGER_FRACTION", "0.5"))
PARTIAL_TP_BREAKEVEN_ON_TRIGGER = os.getenv("PARTIAL_TP_BREAKEVEN_ON_TRIGGER", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Regime-based target scaling (execution-level)
REGIME_TARGET_SCALING_ENABLED = os.getenv("REGIME_TARGET_SCALING_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
REGIME_TARGET_STRONG_TREND_MULT = float(os.getenv("REGIME_TARGET_STRONG_TREND_MULT", "1.5"))
REGIME_TARGET_WEAK_TREND_MULT = float(os.getenv("REGIME_TARGET_WEAK_TREND_MULT", "0.7"))
REGIME_TARGET_RANGE_MULT = float(os.getenv("REGIME_TARGET_RANGE_MULT", "0.7"))
REGIME_TARGET_HIGH_VOL_MULT = float(os.getenv("REGIME_TARGET_HIGH_VOL_MULT", "0.7"))

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

# ── Strict Signal Gates (accuracy-first) ─────────────────────────────────────
# 3-of-5 directional confluence gate: RSI + EMA + MACD + Volume + Divergence
SIGNAL_CONFLUENCE_GATE_ENABLED = os.getenv("SIGNAL_CONFLUENCE_GATE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SIGNAL_CONFLUENCE_MIN_VOTES = int(os.getenv("SIGNAL_CONFLUENCE_MIN_VOTES", "3"))

# Long-term directional hard filter using EMA200
SIGNAL_EMA200_HARD_FILTER_ENABLED = os.getenv("SIGNAL_EMA200_HARD_FILTER_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Require volume confirmation (>= 1.2x 20-bar average where volume exists)
SIGNAL_REQUIRE_VOLUME_GATE = os.getenv("SIGNAL_REQUIRE_VOLUME_GATE", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Require multi-timeframe alignment votes for directional signal emission
MTF_SIGNAL_GATE_ENABLED = os.getenv("MTF_SIGNAL_GATE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
MTF_SIGNAL_MIN_VOTES = int(os.getenv("MTF_SIGNAL_MIN_VOTES", "2"))
MTF_HIGH_CONVICTION_VOTES = int(os.getenv("MTF_HIGH_CONVICTION_VOTES", "3"))

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

# Tradeflow analytics (FII/DII + optional F&O/forex context)
TRADEFLOW_FII_NOISE_THRESHOLD_CR = float(os.getenv("TRADEFLOW_FII_NOISE_THRESHOLD_CR", "500"))
TRADEFLOW_FII_STRONG_THRESHOLD_CR = float(os.getenv("TRADEFLOW_FII_STRONG_THRESHOLD_CR", "3000"))
TRADEFLOW_FIVE_DAY_LOOKBACK = int(os.getenv("TRADEFLOW_FIVE_DAY_LOOKBACK", "5"))
TRADEFLOW_USDINR_EXIT_THRESHOLD_PCT = float(os.getenv("TRADEFLOW_USDINR_EXIT_THRESHOLD_PCT", "0.5"))

# Optional NSE derivatives endpoint path/URL for F&O intent (leave empty to disable)
TRADEFLOW_FO_ENDPOINT = os.getenv("TRADEFLOW_FO_ENDPOINT", "")

# Tradeflow -> signal-score integration (A/B-testable)
TRADEFLOW_SIGNAL_INFLUENCE_ENABLED = os.getenv("TRADEFLOW_SIGNAL_INFLUENCE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
TRADEFLOW_SIGNAL_BIAS_MULTIPLIER = float(os.getenv("TRADEFLOW_SIGNAL_BIAS_MULTIPLIER", "1.15"))
TRADEFLOW_SIGNAL_COUNTER_MULTIPLIER = float(os.getenv("TRADEFLOW_SIGNAL_COUNTER_MULTIPLIER", "0.85"))
TRADEFLOW_SIGNAL_IGNORE_STALE = os.getenv("TRADEFLOW_SIGNAL_IGNORE_STALE", "true").lower() in (
    "1",
    "true",
    "yes",
)
TRADEFLOW_SIGNAL_STALE_DISABLE_HOURS = int(os.getenv("TRADEFLOW_SIGNAL_STALE_DISABLE_HOURS", "24"))

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

# ── Crypto Phase 1 — Accuracy Gates ──────────────────────
# Volume confirmation — LONG/SHORT setup ka entry bar volume must be this
# multiple of the 20-bar rolling average. Default 1.30 = entry bar me 30%
# above-average volume required. Set to 0 to disable the gate entirely.
CRYPTO_VOL_MIN_RATIO = float(os.getenv("CRYPTO_VOL_MIN_RATIO", "1.30"))

# Funding rate hard gates (per-hour funding rate, NOT annualized).
# LONG is REJECTED when funding > CRYPTO_FUNDING_MAX_LONG (crowded longs →
# wash-out risk). SHORT is REJECTED when funding < CRYPTO_FUNDING_MIN_SHORT
# (crowded shorts → squeeze risk). Use 0.0005 (= 0.05 %) as a conservative
# default — any symbol with missing/zero funding data passes unchanged.
CRYPTO_FUNDING_MAX_LONG = float(os.getenv("CRYPTO_FUNDING_MAX_LONG", "0.0005"))
CRYPTO_FUNDING_MIN_SHORT = float(os.getenv("CRYPTO_FUNDING_MIN_SHORT", "-0.0005"))

# Master switches — set to "false" to disable a gate entirely without
# changing the numeric thresholds above (useful for A/B comparisons).
CRYPTO_VOL_GATE_ENABLED = os.getenv("CRYPTO_VOL_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
CRYPTO_FUNDING_GATE_ENABLED = os.getenv("CRYPTO_FUNDING_GATE_ENABLED", "true").lower() in ("1", "true", "yes")

# Multi-timeframe confluence gate (A1). Requires the 4-hour trend to align
# with the 1-hour signal direction:
#   LONG  allowed only if 4h trend != BEARISH
#   SHORT allowed only if 4h trend != BULLISH
# NEUTRAL 4h trend passes (avoids over-blocking during range-bound periods).
# Missing 4h data → gate skipped (same pattern as volume/funding gates).
CRYPTO_MTF_GATE_ENABLED = os.getenv("CRYPTO_MTF_GATE_ENABLED", "true").lower() in ("1", "true", "yes")
# Strict mode = require 4h trend to exactly MATCH the signal direction
# (BULLISH for LONG, BEARISH for SHORT). NEUTRAL 4h → blocked in strict mode.
# Default is relaxed (NEUTRAL passes) so gate is not too aggressive.
CRYPTO_MTF_STRICT = os.getenv("CRYPTO_MTF_STRICT", "false").lower() in ("1", "true", "yes")
# 4h bars to fetch for HTF trend computation. 200 covers ~33 days — enough
# for EMA_200 to be valid and for classify_trend to give a stable label.
CRYPTO_MTF_4H_BARS = int(os.getenv("CRYPTO_MTF_4H_BARS", "200"))

# ── Crypto Phase 1 — Sparse fill ("more results" / B1+B2) ───
# When fewer than CRYPTO_SPARSE_TARGET premium setups pass the strict
# quality/confidence floors, back-fill from a relaxed floor band. Those
# rows are tagged tier="exploratory" vs tier="premium". Accuracy gates
# (A1–A4) still apply — only the score floor is widened.
CRYPTO_QUALITY_FLOOR = int(os.getenv("CRYPTO_QUALITY_FLOOR", "45"))
CRYPTO_CONF_FLOOR = float(os.getenv("CRYPTO_CONF_FLOOR", "35"))
CRYPTO_SPARSE_FILL_ENABLED = os.getenv("CRYPTO_SPARSE_FILL_ENABLED", "true").lower() in ("1", "true", "yes")
CRYPTO_SPARSE_TARGET = int(os.getenv("CRYPTO_SPARSE_TARGET", "5"))
CRYPTO_QUALITY_FLOOR_RELAXED = int(os.getenv("CRYPTO_QUALITY_FLOOR_RELAXED", "40"))
CRYPTO_CONF_FLOOR_RELAXED = float(os.getenv("CRYPTO_CONF_FLOOR_RELAXED", "28"))

# ── Swing outcome tracker (SQLite OPEN rows → SL/T1/T2/EXPIRED) ───
SWING_OUTCOME_EVAL_ENABLED = os.getenv("SWING_OUTCOME_EVAL_ENABLED", "true").lower() in ("1", "true", "yes")
# Minimum seconds between evaluation passes (yFinance budget).
SWING_OUTCOME_EVAL_INTERVAL_SEC = int(os.getenv("SWING_OUTCOME_EVAL_INTERVAL_SEC", "3600"))
SWING_OUTCOME_EVAL_BATCH = int(os.getenv("SWING_OUTCOME_EVAL_BATCH", "120"))
# Calendar days after scan emission before we mark EXPIRED if no level hit.
SWING_OUTCOME_MAX_HOLD_DAYS = int(os.getenv("SWING_OUTCOME_MAX_HOLD_DAYS", "18"))
# Dedicated background loop controls for swing outcome evaluation.
SWING_OUTCOME_TASK_ENABLED = os.getenv("SWING_OUTCOME_TASK_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SWING_OUTCOME_TASK_CHECK_SEC = int(os.getenv("SWING_OUTCOME_TASK_CHECK_SEC", "60"))

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
SWING_VOL_RANGE_BREAKOUT_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_RANGE_BREAKOUT_MIN_ACTIONABLE", "1.5"))
# Hard block: below this ratio, treat breakouts as non-actionable/fakeout-prone.
SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK = float(os.getenv("SWING_VOL_RANGE_BREAKOUT_HARD_BLOCK", "1.5"))

SWING_VOL_INSIDE_BAR     = float(os.getenv("SWING_VOL_INSIDE_BAR",     "2.0"))   # Inside bar breakout
# Minimum ratio for inside-bar setups to be treated as actionable (below => watchlist).
# Raised to 1.5x: anything below this shows as watchlist only (not a live trade signal).
SWING_VOL_INSIDE_BAR_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_INSIDE_BAR_MIN_ACTIONABLE", "1.5"))
# Hard block: vol ratio below this = skip inside-bar signal entirely (not even watchlist).
# e.g. ZYDUSWELL 0.2x -> blocked. Must exceed 0.5x to appear at all.
SWING_VOL_INSIDE_BAR_HARD_BLOCK = float(os.getenv("SWING_VOL_INSIDE_BAR_HARD_BLOCK", "0.5"))

SWING_VOL_BB_SQUEEZE     = float(os.getenv("SWING_VOL_BB_SQUEEZE",     "1.5"))   # BB squeeze expansion
SWING_VOL_BB_SQUEEZE_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_BB_SQUEEZE_MIN_ACTIONABLE", "1.0"))
SWING_VOL_BB_SQUEEZE_HARD_BLOCK = float(os.getenv("SWING_VOL_BB_SQUEEZE_HARD_BLOCK", "0.5"))

SWING_VOL_EMA_PULLBACK   = float(os.getenv("SWING_VOL_EMA_PULLBACK",   "0.8"))   # EMA pullback (min interest)
SWING_VOL_EMA_PULLBACK_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_EMA_PULLBACK_MIN_ACTIONABLE", "0.6"))
SWING_VOL_EMA_PULLBACK_HARD_BLOCK = float(os.getenv("SWING_VOL_EMA_PULLBACK_HARD_BLOCK", "0.3"))

SWING_VOL_52W_BREAKOUT   = float(os.getenv("SWING_VOL_52W_BREAKOUT",   "1.5"))   # 52-week high proximity
SWING_VOL_52W_BREAKOUT_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_52W_BREAKOUT_MIN_ACTIONABLE", "1.0"))
SWING_VOL_52W_BREAKOUT_HARD_BLOCK = float(os.getenv("SWING_VOL_52W_BREAKOUT_HARD_BLOCK", "0.5"))

SWING_VOL_POST_RESULT    = float(os.getenv("SWING_VOL_POST_RESULT",    "3.0"))   # Post-result gap up
SWING_VOL_POST_RESULT_MIN_ACTIONABLE = float(os.getenv("SWING_VOL_POST_RESULT_MIN_ACTIONABLE", "2.0"))
SWING_VOL_POST_RESULT_HARD_BLOCK = float(os.getenv("SWING_VOL_POST_RESULT_HARD_BLOCK", "1.0"))

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

# ── Swing Risk Management (1-3 day holding) ───────────────────────────────
# Virtual capital used for swing sizing calculations (scanner-side guidance).
SWING_VIRTUAL_CAPITAL = float(os.getenv("SWING_VIRTUAL_CAPITAL", "100000"))
# Fractional risk per trade. 0.015 = 1.5% capital risked per setup.
SWING_RISK_PER_TRADE_PCT = float(os.getenv("SWING_RISK_PER_TRADE_PCT", "0.015"))
# Canonical ATR multipliers for swing stop and first target.
SWING_ATR_SL_MULTIPLIER = float(os.getenv("SWING_ATR_SL_MULTIPLIER", "2.0"))
SWING_ATR_TARGET_MULTIPLIER = float(os.getenv("SWING_ATR_TARGET_MULTIPLIER", "3.0"))
# Overnight/portfolio guardrails.
SWING_MAX_CAPITAL_PER_TRADE = float(os.getenv("SWING_MAX_CAPITAL_PER_TRADE", "0.25"))
SWING_MAX_OPEN_POSITIONS = int(os.getenv("SWING_MAX_OPEN_POSITIONS", "5"))
SWING_MAX_SECTOR_CONCENTRATION = int(os.getenv("SWING_MAX_SECTOR_CONCENTRATION", "2"))
SWING_MAX_PORTFOLIO_RISK_PCT = float(os.getenv("SWING_MAX_PORTFOLIO_RISK_PCT", "0.06"))

# Reference index for swing regime gate (must match NSE_SYMBOLS display name).
SWING_INDEX_SYMBOL = os.getenv("SWING_INDEX_SYMBOL", "NIFTY 50").strip() or "NIFTY 50"

# When True, drop LONG swing setups if index htf_trend is BEARISH (and optionally mirror for shorts).
SWING_REGIME_GATE_ENABLED = os.getenv("SWING_REGIME_GATE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
# When True, drop LONG swing setups if index htf_trend is BEARISH.
# DEFAULT: True — protects capital when Nifty 50 is in a confirmed downtrend.
# Individual stocks rarely sustain LONG setups against a falling index.
# Override: set env SWING_BLOCK_LONG_IN_BEARISH_TREND=false to allow LONGs in bear markets.
SWING_BLOCK_LONG_IN_BEARISH_TREND = os.getenv(
    "SWING_BLOCK_LONG_IN_BEARISH_TREND", "true"
).lower() in ("1", "true", "yes")
SWING_BLOCK_SHORT_IN_BULLISH_TREND = os.getenv(
    "SWING_BLOCK_SHORT_IN_BULLISH_TREND", "false"
).lower() in ("1", "true", "yes")
# Choppy-session controls: when benchmark ATR compresses, optionally block new swing entries.
SWING_BLOCK_LONG_IN_CHOPPY_TREND = os.getenv(
    "SWING_BLOCK_LONG_IN_CHOPPY_TREND", "true"
).lower() in ("1", "true", "yes")
SWING_BLOCK_SHORT_IN_CHOPPY_TREND = os.getenv(
    "SWING_BLOCK_SHORT_IN_CHOPPY_TREND", "true"
).lower() in ("1", "true", "yes")
# If latest index ATR is below this fraction of its 20-bar ATR average, mark context as CHOPPY.
SWING_CHOPPY_ATR_RATIO_THRESHOLD = float(
    os.getenv("SWING_CHOPPY_ATR_RATIO_THRESHOLD", "0.7")
)

# ── Dynamic Market Regime Detection ─────────────────────────────────────────
# These settings control how Nifty 50 (^NSEI) daily data is classified into
# TRENDING_BULLISH / TRENDING_BEARISH / SIDEWAYS / MIXED regimes.

# ADX threshold above which market is considered "strongly trending".
# Academic standard: 25. Use 22 for Indian markets (slightly less efficient).
REGIME_ADX_TRENDING_FLOOR = float(os.getenv("REGIME_ADX_TRENDING_FLOOR", "25.0"))

# ADX threshold above which market has some directional bias (MIXED zone).
# Below this = SIDEWAYS (breakout strategies should be avoided).
REGIME_ADX_MIXED_FLOOR = float(os.getenv("REGIME_ADX_MIXED_FLOOR", "20.0"))

# Volatility regime: if current ATR > this ratio × 20-bar ATR avg → HIGH_VOL.
# HIGH_VOL = widen SL, reduce position size.
REGIME_HIGH_VOL_ATR_RATIO = float(os.getenv("REGIME_HIGH_VOL_ATR_RATIO", "1.5"))

# Volatility regime: if current ATR < this ratio × 20-bar ATR avg → LOW_VOL.
# LOW_VOL = possible squeeze/breakout setup forming.
REGIME_LOW_VOL_ATR_RATIO = float(os.getenv("REGIME_LOW_VOL_ATR_RATIO", "0.7"))

# How often to refresh the regime cache (hours). Default 4h = twice a trading day.
# Set lower (e.g., 2) for more frequent updates; set higher (e.g., 8) to reduce yFinance calls.
REGIME_REFRESH_INTERVAL_HOURS = float(os.getenv("REGIME_REFRESH_INTERVAL_HOURS", "4.0"))

# Optional sector hard gate on top of scoring penalties.
SWING_SECTOR_HARD_GATE_ENABLED = os.getenv(
    "SWING_SECTOR_HARD_GATE_ENABLED", "true"
).lower() in ("1", "true", "yes")
# LONGs are blocked when sector momentum is at/below this threshold.
SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD = float(
    os.getenv("SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD", "-1.5")
)
# SHORTs are blocked when sector momentum is at/above this threshold.
SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD = float(
    os.getenv("SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD", "1.5")
)

# Skip scanning a symbol if the last daily bar is older than N calendar days (0 = off).
# Tightened from 10 -> 5: stale data tolerate nahi karna hai for daily accuracy.
SWING_MAX_BAR_AGE_DAYS = float(os.getenv("SWING_MAX_BAR_AGE_DAYS", "5"))

# ── Backtest-Driven Pattern Disable List ──────────────────────────────────────
# Patterns with consistently poor win rates from walk-forward backtesting.
# Names must match EXACTLY what the scanner returns in setup.pattern.
# To re-enable a pattern, remove it from this set or set env var to empty.
#
# Backtest evidence (7 symbols, 90d, 5-day forward check):
#   52-Week High Proximity Breakout          → 0.0%  WR  [DISABLED]
#   52-Week High Proximity Breakout (+1)     → 0.0%  WR  [DISABLED]
#   BB Squeeze Breakout (Bullish)            → 0.0%  WR  [DISABLED]
#   BB Squeeze Breakout (Bearish)            → 0.0%  WR  [DISABLED]
#   Pullback to EMA20 (Uptrend)              → 18.8% WR  [DISABLED]
#   Rally to EMA20 (Downtrend)               → 18.5% WR  [DISABLED]
#   Bearish RSI Divergence                   → 11.8% WR  [DISABLED]
#
# To override via env: SWING_DISABLED_PATTERNS="" (empty = none disabled)
_disabled_env = os.getenv("SWING_DISABLED_PATTERNS", "DEFAULT")
if _disabled_env == "DEFAULT":
    SWING_DISABLED_PATTERNS: set[str] = {
        "52-Week High Proximity Breakout",
        "52-Week High Proximity Breakout (+1 confluent)",
        "52-Week High Proximity Breakout (Watchlist)",
        "BB Squeeze Breakout (Bullish)",
        "BB Squeeze Breakout (Bearish)",
        "BB Squeeze Breakout (Bullish) (Watchlist)",
        "BB Squeeze Breakout (Bearish) (Watchlist)",
        "Pullback to EMA20 (Uptrend)",
        "Pullback to EMA20 (Uptrend) (+1 confluent)",
        "Rally to EMA20 (Downtrend)",
        "Rally to EMA20 (Downtrend) (+1 confluent)",
        "Bearish RSI Divergence",
    }
elif _disabled_env.strip() == "":
    SWING_DISABLED_PATTERNS = set()
else:
    SWING_DISABLED_PATTERNS = {p.strip() for p in _disabled_env.split(",") if p.strip()}

# Last updated: 2026-04-28 (based on 90-day walk-forward backtest)
# Re-run scripts/backtester.py with 180d --forward 7 to refresh these verdicts.

# ── Phase 1 quality gates (accuracy improvements) ────────────────────────────
# Use N-day rolling avg for "recent" volume instead of last single bar.
# Single-bar volume ratio ek din ke spike se manipulate ho jaata hai; 3-day smooths.
SWING_VOL_ROLLING_DAYS = int(os.getenv("SWING_VOL_ROLLING_DAYS", "3"))

# ADX hard gate for breakout patterns. Below this = no trend = breakout likely fails.
# Applies to: 20-day range breakout, 52W high, BB squeeze, inside-bar breakout.
# 0 disables the gate. 18 is the academic "weak trend" floor; 20 is stricter.
SWING_ADX_MIN_FOR_BREAKOUT = float(os.getenv("SWING_ADX_MIN_FOR_BREAKOUT", "18"))

# Golden/Death Cross needs enough history for EMA_200 to be meaningful.
# yFinance ke early bars me EMA_200 garbage hota hai — false crosses.
SWING_MIN_BARS_FOR_EMA200 = int(os.getenv("SWING_MIN_BARS_FOR_EMA200", "220"))
SWING_MIN_CLEAN_EMA200_TAIL = int(os.getenv("SWING_MIN_CLEAN_EMA200_TAIL", "20"))

# Require 2 consecutive closes beyond the breakout level (range high/low, 52W high).
# Single-bar breakouts ka failure rate zyada. Off by default so behaviour backwards compatible —
# enable for production via env.
SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM = os.getenv(
    "SWING_REQUIRE_2BAR_BREAKOUT_CONFIRM", "true"
).lower() in ("1", "true", "yes")

# News context: decouple from strength. If True (default), news score only affects grade,
# not raw strength. Legacy behaviour (penalise strength) available via env=false.
SWING_NEWS_AFFECTS_STRENGTH = os.getenv(
    "SWING_NEWS_AFFECTS_STRENGTH", "false"
).lower() in ("1", "true", "yes")

# Sector momentum aggregation: "median" (robust to outliers) or "mean" (legacy).
SWING_SECTOR_MOMENTUM_AGG = os.getenv("SWING_SECTOR_MOMENTUM_AGG", "median").lower()

# Number of swing setups returned by API cache (higher = fewer names get hidden).
SWING_MAX_SETUPS_RETURN = int(os.getenv("SWING_MAX_SETUPS_RETURN", "100"))

# Keep watchlist-grade setups visually below full actionable conviction.
SWING_WATCHLIST_MAX_STRENGTH = int(os.getenv("SWING_WATCHLIST_MAX_STRENGTH", "72"))

# Portfolio swing scanner output (dashboard shortlist): include watchlist rows or not.
# Default false keeps the feed actionable; single-symbol lookup still supports watchlist/fallback.
SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN = os.getenv(
    "SWING_INCLUDE_WATCHLIST_IN_PORTFOLIO_SCAN", "false"
).lower() in ("1", "true", "yes")

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

# Swing MTF confirmation gate (higher-timeframe direction check before keeping setups).
# Uses provided `mtf_trend` hint or `htf_trend`/`mtf_score` columns when available.
SWING_MTF_CONFIRM_ENABLED = os.getenv("SWING_MTF_CONFIRM_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
SWING_MTF_ALLOW_NEUTRAL = os.getenv("SWING_MTF_ALLOW_NEUTRAL", "true").lower() in (
    "1",
    "true",
    "yes",
)
SWING_MTF_BLOCK_ON_UNKNOWN = os.getenv("SWING_MTF_BLOCK_ON_UNKNOWN", "false").lower() in (
    "1",
    "true",
    "yes",
)
SWING_MTF_NEUTRAL_STRENGTH_PENALTY = int(os.getenv("SWING_MTF_NEUTRAL_STRENGTH_PENALTY", "4"))
SWING_MTF_SCORE_NEUTRAL_BAND = float(os.getenv("SWING_MTF_SCORE_NEUTRAL_BAND", "15"))

# Peak-based RSI divergence tuning for swing scanner.
SWING_RSI_DIVERGENCE_LOOKBACK = int(os.getenv("SWING_RSI_DIVERGENCE_LOOKBACK", "35"))
SWING_RSI_DIVERGENCE_PIVOT_WINDOW = int(os.getenv("SWING_RSI_DIVERGENCE_PIVOT_WINDOW", "2"))
SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT = float(
    os.getenv("SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT", "0.4")
)
SWING_RSI_DIVERGENCE_MIN_RSI_DELTA = float(os.getenv("SWING_RSI_DIVERGENCE_MIN_RSI_DELTA", "2.0"))

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

# ── Forex Module ─────────────────────────────────────────────────────────────

TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")

FOREX_SYMBOLS: list = [
    # INR Pairs — trade on NSE 9:00 AM to 5:00 PM IST
    "USD/INR", "EUR/INR", "GBP/INR", "JPY/INR",
    # Major Global Pairs — 24hr but best during sessions
    "EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD",
]

FOREX_TIMEFRAMES: list = ["1h", "4h", "1day"]

# Market sessions in IST (UTC+5:30)
FOREX_SESSIONS: dict = {
    "ASIAN":   {"start": "05:30", "end": "14:30",
                "best_pairs": ["USD/JPY", "AUD/USD"]},
    "LONDON":  {"start": "13:30", "end": "22:30",
                "best_pairs": ["EUR/USD", "GBP/USD", "EUR/INR"]},
    "NEWYORK": {"start": "18:30", "end": "03:30",
                "best_pairs": ["EUR/USD", "USD/CAD", "GBP/USD"]},
    "NSE_INR": {"start": "09:00", "end": "17:00",
                "best_pairs": ["USD/INR", "EUR/INR", "GBP/INR"]},
}

FOREX_PROFILES: dict = {
    "USD/INR": {
        "max_sl_pips": 30,
        "min_rr": 1.5,
        "rsi_buy_zone": [38, 58],
        "best_patterns": ["EMA20_PULLBACK", "SUPPORT_REVERSAL", "RANGE_BREAKOUT"],
        "hold_hours": [24, 72],
        "session": "NSE_INR",
    },
    "EUR/INR": {
        "max_sl_pips": 35,
        "min_rr": 1.5,
        "rsi_buy_zone": [38, 58],
        "best_patterns": ["EMA20_PULLBACK", "SUPPORT_REVERSAL", "RANGE_BREAKOUT"],
        "hold_hours": [24, 72],
        "session": "NSE_INR",
    },
    "GBP/INR": {
        "max_sl_pips": 40,
        "min_rr": 1.5,
        "rsi_buy_zone": [38, 58],
        "best_patterns": ["EMA20_PULLBACK", "RANGE_BREAKOUT"],
        "hold_hours": [24, 72],
        "session": "NSE_INR",
    },
    "JPY/INR": {
        "max_sl_pips": 30,
        "min_rr": 1.5,
        "rsi_buy_zone": [38, 58],
        "best_patterns": ["EMA20_PULLBACK", "SUPPORT_REVERSAL"],
        "hold_hours": [24, 72],
        "session": "NSE_INR",
    },
    "EUR/USD": {
        "max_sl_pips": 50,
        "min_rr": 1.5,
        "rsi_buy_zone": [40, 62],
        "best_patterns": ["EMA20_PULLBACK", "BB_SQUEEZE", "RSI_DIVERGENCE"],
        "hold_hours": [48, 168],
        "session": "LONDON",
    },
    "GBP/USD": {
        "max_sl_pips": 60,
        "min_rr": 1.8,
        "rsi_buy_zone": [38, 60],
        "best_patterns": ["RANGE_BREAKOUT", "INSIDE_BAR", "SUPPORT_REVERSAL"],
        "hold_hours": [48, 120],
        "session": "LONDON",
    },
    "USD/JPY": {
        "max_sl_pips": 40,
        "min_rr": 1.5,
        "rsi_buy_zone": [42, 62],
        "best_patterns": ["EMA20_PULLBACK", "SUPPORT_REVERSAL"],
        "hold_hours": [24, 96],
        "session": "ASIAN",
    },
    "AUD/USD": {
        "max_sl_pips": 40,
        "min_rr": 1.5,
        "rsi_buy_zone": [40, 60],
        "best_patterns": ["EMA20_PULLBACK", "BB_SQUEEZE"],
        "hold_hours": [48, 120],
        "session": "ASIAN",
    },
    "USD/CAD": {
        "max_sl_pips": 45,
        "min_rr": 1.5,
        "rsi_buy_zone": [40, 60],
        "best_patterns": ["EMA20_PULLBACK", "RANGE_BREAKOUT"],
        "hold_hours": [48, 120],
        "session": "NEWYORK",
    },
}

FOREX_CACHE_MINUTES: int = 60
FOREX_RATE_REFRESH_MIN: int = 15
TWELVE_DATA_DAILY_LIMIT: int = 800
FOREX_SIGNAL_STRENGTH_THRESHOLD: int = 50
FOREX_ALERT_STRENGTH_THRESHOLD: int = 70
FOREX_MAX_LOT_SIZE: float = 1.0
FOREX_RISK_PCT_PER_TRADE: float = 0.01
FOREX_SCAN_INTERVAL_MINUTES: int = 60

# ── Edge Engine (Macro Intelligence Layer) ───────────────────────────────────
# Zero-cost macro/fundamental intelligence layer that modifies technical signal confidence.
# All APIs are free: FRED (macro), CFTC (COT), public sentiment, market risk gauges.

# FRED API (Federal Reserve Economic Data) — free from stlouisfed.org
FRED_API_KEY: str = os.getenv("FRED_API_KEY", "")

# Edge Engine cache TTLs (seconds)
EDGE_MACRO_CACHE_TTL: int = 86400      # 24 hours for macro indicators
EDGE_COT_CACHE_TTL: int = 604800       # 7 days for CFTC COT data (weekly release)
EDGE_SENTIMENT_CACHE_TTL: int = 3600   # 1 hour for retail sentiment
EDGE_RISK_CACHE_TTL: int = 3600        # 1 hour for risk indicators
EDGE_SEASONAL_CACHE_TTL: int = 2592000 # 30 days for seasonality patterns
EDGE_CARRY_CACHE_TTL: int = 86400      # 24 hours for carry spreads
EDGE_SCORE_CACHE_TTL: int = 3600       # 1 hour for final EdgeScore

# EdgeScore component weights (must sum to 1.0 before bonuses)
EDGE_WEIGHT_MACRO: float = 0.30        # Macro environment (GDP, CPI, rates)
EDGE_WEIGHT_COT: float = 0.25          # Institutional positioning
EDGE_WEIGHT_SENTIMENT: float = 0.20    # Retail sentiment (contrarian)
EDGE_WEIGHT_RISK: float = 0.10         # Risk-on/risk-off gauge
EDGE_WEIGHT_MOMENTUM: float = 0.15     # Market momentum (placeholder for now)

# EdgeScore confidence modifiers
EDGE_STRONG_BULLISH_THRESHOLD: float = 70.0   # EdgeScore >= 70 = strong bullish
EDGE_STRONG_BEARISH_THRESHOLD: float = -70.0  # EdgeScore <= -70 = strong bearish
EDGE_ALIGNED_CONF_BOOST: float = 0.15         # +15% confidence when edge aligns with signal
EDGE_CONFLICT_CONF_PENALTY: float = 0.20      # -20% confidence when edge conflicts

# Macro scoring thresholds
MACRO_MIN_INDICATORS: int = 3          # Minimum indicators required (out of 5)
MACRO_STRONG_GROWTH_GDP: float = 3.0   # GDP growth > 3% YoY = strong
MACRO_LOW_INFLATION_CPI: float = 3.0   # CPI < 3% YoY = low inflation
MACRO_HIGH_INFLATION_CPI: float = 5.0  # CPI > 5% YoY = high inflation
MACRO_LOW_RATES_FED: float = 2.0       # Fed Funds < 2% = low rates
MACRO_HIGH_RATES_FED: float = 5.0      # Fed Funds > 5% = high rates

# FRED Series IDs
FRED_SERIES_GDP: str = "GDPC1"         # Real GDP (quarterly)
FRED_SERIES_CPI: str = "CPIAUCSL"      # CPI All Urban (monthly)
FRED_SERIES_UNEMPLOYMENT: str = "UNRATE"  # Unemployment rate (monthly)
FRED_SERIES_FED_FUNDS: str = "DFF"     # Fed Funds rate (daily)
FRED_SERIES_TREASURY_10Y: str = "DGS10"  # 10-year Treasury yield (daily)

# Cache file paths
EDGE_MACRO_CACHE_FILE: str = "macro_cache.json"
EDGE_COT_CACHE_FILE: str = "cot_cache.json"
EDGE_SENTIMENT_CACHE_FILE: str = "sentiment_cache.json"
EDGE_RISK_CACHE_FILE: str = "risk_cache.json"
EDGE_SEASONAL_CACHE_FILE: str = "seasonal_cache.json"
EDGE_CARRY_CACHE_FILE: str = "carry_cache.json"
EDGE_SCORES_CACHE_FILE: str = "edge_scores_cache.json"

# Fallback behavior
EDGE_FALLBACK_ENABLED: bool = True     # Use stale cache on API failure

# V2 Targeted News & Sentiment Gate
SENTIMENT_GATE_ENABLED: bool = os.getenv("SENTIMENT_GATE_ENABLED", "true").lower() in ("true", "1")


def validate_runtime_config() -> None:
    """Fail fast on invalid runtime settings that can break refresh/signals."""
    errors: list[str] = []

    if not DATABASE_URL or "://" not in DATABASE_URL:
        errors.append("DATABASE_URL must be a valid SQLAlchemy URL")

    if not NSE_SYMBOLS and not CRYPTO_SYMBOLS:
        errors.append("At least one symbol universe must be configured (NSE_SYMBOLS or CRYPTO_SYMBOLS)")

    if MIN_RISK_REWARD <= 0:
        errors.append("MIN_RISK_REWARD must be > 0")

    if INTRADAY_MAX_BAR_AGE_MINUTES < 0:
        errors.append("INTRADAY_MAX_BAR_AGE_MINUTES must be >= 0")

    if INTRADAY_VIRTUAL_CAPITAL <= 0:
        errors.append("INTRADAY_VIRTUAL_CAPITAL must be > 0")

    if INTRADAY_RISK_PER_TRADE_PCT <= 0 or INTRADAY_RISK_PER_TRADE_PCT > 100:
        errors.append("INTRADAY_RISK_PER_TRADE_PCT must be in (0, 100]")

    if INTRADAY_ATR_SL_MULTIPLIER <= 0:
        errors.append("INTRADAY_ATR_SL_MULTIPLIER must be > 0")

    if INTRADAY_TARGET_ATR_MULTIPLIER <= 0:
        errors.append("INTRADAY_TARGET_ATR_MULTIPLIER must be > 0")

    if INTRADAY_MIN_RISK_REWARD <= 0:
        errors.append("INTRADAY_MIN_RISK_REWARD must be > 0")

    if INTRADAY_MAX_OPEN_TRADES < 1:
        errors.append("INTRADAY_MAX_OPEN_TRADES must be >= 1")

    if INTRADAY_MAX_DAILY_LOSS_PCT <= 0 or INTRADAY_MAX_DAILY_LOSS_PCT >= 1:
        errors.append("INTRADAY_MAX_DAILY_LOSS_PCT must be between 0 and 1")

    if INTRADAY_MAX_SINGLE_TRADE_PCT <= 0 or INTRADAY_MAX_SINGLE_TRADE_PCT > 1:
        errors.append("INTRADAY_MAX_SINGLE_TRADE_PCT must be in (0, 1]")

    if not (0 <= INTRADAY_SQUARE_OFF_HOUR_IST <= 23):
        errors.append("INTRADAY_SQUARE_OFF_HOUR_IST must be between 0 and 23")

    if not (0 <= INTRADAY_SQUARE_OFF_MINUTE_IST <= 59):
        errors.append("INTRADAY_SQUARE_OFF_MINUTE_IST must be between 0 and 59")

    if INTRADAY_MIN_RVOL < 0:
        errors.append("INTRADAY_MIN_RVOL must be >= 0")

    if not (0 <= INTRADAY_ENTRY_CUTOFF_HOUR_IST <= 23):
        errors.append("INTRADAY_ENTRY_CUTOFF_HOUR_IST must be between 0 and 23")

    if not (0 <= INTRADAY_ENTRY_CUTOFF_MINUTE_IST <= 59):
        errors.append("INTRADAY_ENTRY_CUTOFF_MINUTE_IST must be between 0 and 59")

    mtf_sum = sum(float(v) for v in MTF_WEIGHTS.values()) if MTF_WEIGHTS else 0.0
    if abs(mtf_sum - 1.0) > 1e-6:
        errors.append(f"MTF_WEIGHTS must sum to 1.0 (got {mtf_sum:.6f})")

    if NSE_SYMBOL_TIMEOUT_SECONDS <= 0:
        errors.append("NSE_SYMBOL_TIMEOUT_SECONDS must be > 0")

    if REFRESH_INTERVAL_SEC < 10:
        errors.append("REFRESH_INTERVAL_SEC must be >= 10")

    if REFRESH_CYCLE_TIMEOUT_SEC < 30:
        errors.append("REFRESH_CYCLE_TIMEOUT_SEC must be >= 30")

    if BACKGROUND_READY_WAIT_SEC < 30:
        errors.append("BACKGROUND_READY_WAIT_SEC must be >= 30")

    if SWING_OUTCOME_EVAL_INTERVAL_SEC < 60:
        errors.append("SWING_OUTCOME_EVAL_INTERVAL_SEC must be >= 60")

    if SWING_OUTCOME_TASK_CHECK_SEC < 30:
        errors.append("SWING_OUTCOME_TASK_CHECK_SEC must be >= 30")

    if SWING_VIRTUAL_CAPITAL <= 0:
        errors.append("SWING_VIRTUAL_CAPITAL must be > 0")

    if SWING_RISK_PER_TRADE_PCT <= 0 or SWING_RISK_PER_TRADE_PCT > 1:
        errors.append("SWING_RISK_PER_TRADE_PCT must be in (0, 1]")

    if SWING_ATR_SL_MULTIPLIER <= 0:
        errors.append("SWING_ATR_SL_MULTIPLIER must be > 0")

    if SWING_ATR_TARGET_MULTIPLIER <= 0:
        errors.append("SWING_ATR_TARGET_MULTIPLIER must be > 0")

    if SWING_MAX_CAPITAL_PER_TRADE <= 0 or SWING_MAX_CAPITAL_PER_TRADE > 1:
        errors.append("SWING_MAX_CAPITAL_PER_TRADE must be in (0, 1]")

    if SWING_MAX_OPEN_POSITIONS < 1:
        errors.append("SWING_MAX_OPEN_POSITIONS must be >= 1")

    if SWING_MAX_SECTOR_CONCENTRATION < 1:
        errors.append("SWING_MAX_SECTOR_CONCENTRATION must be >= 1")

    if SWING_MAX_PORTFOLIO_RISK_PCT <= 0 or SWING_MAX_PORTFOLIO_RISK_PCT > 1:
        errors.append("SWING_MAX_PORTFOLIO_RISK_PCT must be in (0, 1]")

    if SWING_CHOPPY_ATR_RATIO_THRESHOLD <= 0:
        errors.append("SWING_CHOPPY_ATR_RATIO_THRESHOLD must be > 0")

    if SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD >= 0:
        errors.append("SWING_SECTOR_HEADWIND_BLOCK_THRESHOLD must be < 0")

    if SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD <= 0:
        errors.append("SWING_SECTOR_TAILWIND_BLOCK_SHORT_THRESHOLD must be > 0")

    if SWING_MTF_NEUTRAL_STRENGTH_PENALTY < 0:
        errors.append("SWING_MTF_NEUTRAL_STRENGTH_PENALTY must be >= 0")

    if SWING_MTF_SCORE_NEUTRAL_BAND < 0:
        errors.append("SWING_MTF_SCORE_NEUTRAL_BAND must be >= 0")

    if SWING_RSI_DIVERGENCE_LOOKBACK < 10:
        errors.append("SWING_RSI_DIVERGENCE_LOOKBACK must be >= 10")

    if SWING_RSI_DIVERGENCE_PIVOT_WINDOW < 1:
        errors.append("SWING_RSI_DIVERGENCE_PIVOT_WINDOW must be >= 1")

    if SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT < 0:
        errors.append("SWING_RSI_DIVERGENCE_MIN_PRICE_DELTA_PCT must be >= 0")

    if SWING_RSI_DIVERGENCE_MIN_RSI_DELTA < 0:
        errors.append("SWING_RSI_DIVERGENCE_MIN_RSI_DELTA must be >= 0")

    if CONTEXT_NEWS_WEIGHT_24HR < 0 or CONTEXT_NEWS_WEIGHT_7D < 0:
        errors.append("CONTEXT_NEWS_WEIGHT_24HR and CONTEXT_NEWS_WEIGHT_7D must be >= 0")

    if (CONTEXT_NEWS_WEIGHT_24HR + CONTEXT_NEWS_WEIGHT_7D) > 1.0:
        errors.append("CONTEXT_NEWS_WEIGHT_24HR + CONTEXT_NEWS_WEIGHT_7D must be <= 1.0")

    if IS_PRODUCTION:
        if not DATABASE_URL or not any(DATABASE_URL.startswith(p) for p in ("postgresql://", "postgres://", "postgresql+psycopg://", "postgresql+psycopg2://")):
            errors.append(f"In production, DATABASE_URL must be set and must point to persistent PostgreSQL (got: {DATABASE_URL})")
        if not ADMIN_API_KEY:
            errors.append("In production, ADMIN_API_KEY must be configured for API security")

    if errors:
        raise ValueError("Invalid runtime config: " + "; ".join(errors))


# ── SwingSpectrum Paper Trading Mode ─────────────────────────────────────────
# Auto-captures every swing signal into a virtual journal and checks outcomes.
# Builds live win-rate proof BEFORE real capital is deployed.
#
# PAPER_TRADING_ENABLED=true   → signals auto-captured on every scan
# PAPER_TRADING_ENABLED=false  → paper trading off (default safe mode)
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "true").lower() in (
    "1", "true", "yes"
)

# Capital used for paper trade position sizing (virtual, no real money)
PAPER_TRADING_CAPITAL = float(os.getenv("PAPER_TRADING_CAPITAL", "500000.0"))  # 5 lakh

# Unlock thresholds: system will show "VALIDATED — ready for real trading"
# only when BOTH conditions are met (run enough trades + high enough win rate)
PAPER_TRADING_UNLOCK_MIN_TRADES = int(os.getenv("PAPER_TRADING_UNLOCK_MIN_TRADES", "30"))
PAPER_TRADING_UNLOCK_MIN_WR_PCT = float(os.getenv("PAPER_TRADING_UNLOCK_MIN_WR_PCT", "50.0"))

# How many calendar days ahead to check SL/Target for EOD resolution
PAPER_TRADING_FORWARD_DAYS = int(os.getenv("PAPER_TRADING_FORWARD_DAYS", "5"))

# Max open paper trades at once (prevents over-crowding)
PAPER_TRADING_MAX_OPEN = int(os.getenv("PAPER_TRADING_MAX_OPEN", "5"))

# ── Position Sizing & Margin Bounds ──────────────────────────────────────────
MAX_POSITION_LEVERAGE: float = float(os.getenv("MAX_POSITION_LEVERAGE", "0.25"))
MAX_PORTFOLIO_LEVERAGE: float = float(os.getenv("MAX_PORTFOLIO_LEVERAGE", "1.0"))


# ── Supabase Cloud Configuration ─────────────────────────────────────────────
SUPABASE_URL: str = os.getenv("SUPABASE_URL", "")
SUPABASE_PUBLISHABLE_KEY: str = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
SUPABASE_SECRET_KEY: str = os.getenv("SUPABASE_SECRET_KEY", "")
SUPABASE_JWKS_URL: str = os.getenv("SUPABASE_JWKS_URL", "")
