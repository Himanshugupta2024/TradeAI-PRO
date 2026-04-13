"""Telegram alert system for TradeAI PRO.

Completely free — uses Telegram Bot API (no paid tier).

Setup (one-time, 2 minutes):
1. Open Telegram → search @BotFather → /newbot → copy the TOKEN
2. Open your bot, send any message, then visit:
   https://api.telegram.org/bot<TOKEN>/getUpdates
   Copy your "chat_id" from the response.
3. Add to your .env:
   TELEGRAM_BOT_TOKEN=your_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
4. Optional: TELEGRAM_ALERTS_ENABLED=true (default: false until configured)

The module silently no-ops if env vars are missing — safe to deploy.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from db.connection import SessionLocal
from db.models import TelegramAlertLog

logger = logging.getLogger(__name__)

_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
_ENABLED     = os.getenv("TELEGRAM_ALERTS_ENABLED", "false").lower() == "true"
_API_BASE    = "https://api.telegram.org/bot"

# Minimum gap between alerts for the same symbol (prevents rapid-fire spam)
_DEDUP_MINUTES = int(os.getenv("TELEGRAM_DEDUP_MINUTES", "60"))

# Signal emojis
_SIGNAL_EMOJI = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⏸"}
_RISK_EMOJI   = {"HIGH": "⚠️", "MEDIUM": "🔶", "LOW": "🔷"}


def is_configured() -> bool:
    """Return True only when bot token and chat ID are both set."""
    return bool(_BOT_TOKEN and _CHAT_ID)


def _already_sent(symbol: str, signal: str, within_minutes: int) -> bool:
    """Check DB to avoid sending duplicate alerts too soon."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    try:
        with SessionLocal() as db:
            exists = (
                db.query(TelegramAlertLog)
                .filter(
                    TelegramAlertLog.symbol == symbol,
                    TelegramAlertLog.signal == signal,
                    TelegramAlertLog.sent_at >= cutoff.replace(tzinfo=None),
                )
                .first()
            )
            return exists is not None
    except Exception:
        return False


def _log_sent(symbol: str, signal: str, message: str, delivered: bool) -> None:
    try:
        with SessionLocal() as db:
            db.add(TelegramAlertLog(
                symbol=symbol,
                signal=signal,
                message=message[:2000],
                delivered=1 if delivered else 0,
            ))
            db.commit()
    except Exception as e:
        logger.debug("TelegramAlertLog write failed: %s", e)


def send_signal_alert(
    symbol: str,
    signal: str,
    price: float,
    confidence: float,
    rsi: Optional[float] = None,
    change_24h: Optional[float] = None,
    volume_spike: Optional[float] = None,
    news_sentiment: Optional[str] = None,
    reason: Optional[str] = None,
    risk_level: str = "HIGH",
) -> bool:
    """
    Send a BUY/SELL/HOLD signal alert to Telegram.

    Returns True if sent successfully, False otherwise.
    Silently returns False if not configured or disabled.
    """
    if not _ENABLED or not is_configured():
        return False

    # Dedup — skip if same signal sent for this coin recently
    if _already_sent(symbol, signal, _DEDUP_MINUTES):
        logger.debug("Telegram dedup: %s %s skipped (sent within %dm)", signal, symbol, _DEDUP_MINUTES)
        return False

    sig_emoji  = _SIGNAL_EMOJI.get(signal, "🔔")
    risk_emoji = _RISK_EMOJI.get(risk_level, "⚠️")

    # Format price nicely (meme coins can be very small)
    if price >= 1000:
        price_str = f"${price:,.2f}"
    elif price >= 1:
        price_str = f"${price:.4f}"
    else:
        price_str = f"${price:.6f}"

    change_str  = f"{'+' if (change_24h or 0) >= 0 else ''}{change_24h:.2f}%" if change_24h is not None else "—"
    rsi_str     = f"{rsi:.1f}" if rsi is not None else "—"
    vol_str     = f"{volume_spike:.1f}x {'🔥' if volume_spike >= 2 else ''}" if volume_spike else "—"
    news_str    = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish", "neutral": "⚫ Neutral"}.get(news_sentiment or "", "—")
    reason_str  = f"\n📋 *Reason:* {reason}" if reason else ""

    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    time_str = now_ist.strftime("%d %b %I:%M %p IST")

    message = (
        f"{sig_emoji} *{signal} Signal — {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Price:* {price_str}\n"
        f"📈 *24h Change:* {change_str}\n"
        f"📊 *RSI:* {rsi_str}\n"
        f"🌊 *Volume:* {vol_str}\n"
        f"📰 *News:* {news_str}\n"
        f"🎯 *AI Confidence:* {int(confidence * 100)}%\n"
        f"{risk_emoji} *Risk:* {risk_level}"
        f"{reason_str}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {time_str}\n"
        f"_TradeAI PRO — Use small position sizes for meme coins_"
    )

    url = f"{_API_BASE}{_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    _CHAT_ID,
        "text":       message,
        "parse_mode": "Markdown",
    }

    delivered = False
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        delivered = True
        logger.info("Telegram alert sent: %s %s @ %s", signal, symbol, price_str)
    except Exception as e:
        logger.warning("Telegram alert failed for %s %s: %s", signal, symbol, e)

    _log_sent(symbol, signal, message, delivered)
    return delivered


def send_whale_alert(symbol: str, price: float, volume_spike: float) -> bool:
    """Send a whale/volume spike alert (2x+ volume)."""
    if not _ENABLED or not is_configured():
        return False

    if _already_sent(symbol, f"WHALE_{symbol}", within_minutes=30):
        return False

    if price >= 1:
        price_str = f"${price:.4f}"
    else:
        price_str = f"${price:.6f}"

    message = (
        f"🐋 *Volume SPIKE — {symbol}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *Price:* {price_str}\n"
        f"🌊 *Volume:* {volume_spike:.1f}x average 🔥\n"
        f"⚠️ Potential whale accumulation or pump — watch RSI\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"_TradeAI PRO — Not financial advice_"
    )

    url = f"{_API_BASE}{_BOT_TOKEN}/sendMessage"
    delivered = False
    try:
        resp = requests.post(url, json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        resp.raise_for_status()
        delivered = True
    except Exception as e:
        logger.warning("Telegram whale alert failed: %s", e)

    _log_sent(symbol, f"WHALE_{symbol}", message, delivered)
    return delivered


def send_meme_season_alert(index: float, status: str) -> bool:
    """Alert when Meme Season activates or ends."""
    if not _ENABLED or not is_configured():
        return False

    if _already_sent("GLOBAL", "MEME_SEASON", within_minutes=360):
        return False

    emoji = "🚀" if status == "ACTIVE" else "😴"
    message = (
        f"{emoji} *Meme Season — {status}*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *Meme Season Index:* {index:.0f}/100\n"
        f"{'🔥 Alt coins are outperforming BTC — meme coins may pump!' if status == 'ACTIVE' else '📉 BTC dominance rising — meme coins may bleed.'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"_TradeAI PRO — Use strict position sizing_"
    )

    url = f"{_API_BASE}{_BOT_TOKEN}/sendMessage"
    delivered = False
    try:
        resp = requests.post(url, json={"chat_id": _CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=10)
        resp.raise_for_status()
        delivered = True
    except Exception as e:
        logger.warning("Telegram meme season alert failed: %s", e)

    _log_sent("GLOBAL", "MEME_SEASON", message, delivered)
    return delivered
