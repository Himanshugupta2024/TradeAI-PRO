"""
Telegram Alert Bot.

Sends trading signals to a Telegram channel or group.
Free to use — requires a Bot Token from @BotFather
and a Chat ID (group/channel ID).

Supports:
  - Signal alerts (BUY/SELL)
  - Trade closed alerts
  - Daily summary
  - Weekly digest
  - Regime change alerts
  - Health/error notifications
"""

import os
import urllib.request
import urllib.parse
import json
from datetime import datetime

from config.logger import get_logger

log = get_logger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_bot_validated = False


def validate_bot() -> bool:
    """Validate that the Telegram bot token and chat ID are set and working."""
    global _bot_validated
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars")
        return False

    if _bot_validated:
        return True

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                bot_name = result.get("result", {}).get("username", "unknown")
                log.info("Telegram bot validated: @%s", bot_name)
                _bot_validated = True
                return True
            else:
                log.error("Telegram bot validation failed: %s", result)
                return False
    except Exception as e:
        log.error("Telegram bot validation error: %s", e)
        return False


def _send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram Bot API (no external dependency)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram skipped — TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                log.info("Telegram alert sent")
                return True
            else:
                log.error("Telegram API error: %s", result)
                return False
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False


def send_signal_alert(
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float,
    target: float,
    confidence: float,
    trend: str = "",
    rsi: float = 0,
    regime: str = "",
    mtf_score: float = None,
    edge_score: float = None,
) -> bool:
    """Send a formatted trading signal alert."""
    emoji = "🟢" if direction in ("LONG", "BUY") else "🔴"
    now = datetime.now().strftime("%d %b %Y, %I:%M %p IST")

    rr = 0
    if direction in ("LONG", "BUY") and entry > stop_loss:
        rr = (target - entry) / (entry - stop_loss)
    elif direction in ("SHORT", "SELL") and stop_loss > entry:
        rr = (entry - target) / (stop_loss - entry)

    text = (
        f"{emoji} <b>{direction} Signal — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: <code>₹{entry:,.2f}</code>\n"
        f"🛑 Stop Loss: <code>₹{stop_loss:,.2f}</code>\n"
        f"🎯 Target: <code>₹{target:,.2f}</code>\n"
        f"📊 R:R = <code>1:{rr:.1f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 AI Confidence: <b>{confidence:.0f}%</b>\n"
    )

    if regime:
        text += f"🌡️ Regime: <b>{regime}</b>\n"

    if mtf_score is not None:
        mtf_emoji = "📈" if mtf_score > 0 else "📉" if mtf_score < 0 else "➡️"
        text += f"{mtf_emoji} MTF Score: <b>{mtf_score:+.0f}</b>\n"

    if edge_score is not None:
        edge_emoji = "🟢" if edge_score > 50 else "🟡" if edge_score > 0 else "🔴"
        text += f"{edge_emoji} EdgeScore: <b>{edge_score:+.0f}</b>\n"

    if trend:
        text += f"📈 Trend: <b>{trend}</b>"
        if rsi:
            text += f" | RSI: <b>{rsi:.1f}</b>"
        text += "\n"

    text += f"⏰ {now}"

    return _send_message(text)


def send_trade_closed_alert(
    symbol: str,
    direction: str,
    entry: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
) -> bool:
    """Send alert when a trade is closed."""
    emoji = "🎯" if pnl > 0 else "❌"
    pnl_emoji = "📈" if pnl > 0 else "📉"
    now = datetime.now().strftime("%d %b %Y, %I:%M %p IST")

    # Friendly exit reason
    reason_map = {
        "CLOSED_TP": "🎯 Target Hit",
        "CLOSED_SL": "🛑 Stop Loss",
        "CLOSED_TRAIL": "📐 Trailing Stop",
        "CLOSED_MANUAL": "✋ Manual Close",
    }
    display_reason = reason_map.get(exit_reason, exit_reason)

    text = (
        f"{emoji} <b>Trade Closed — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Entry: <code>₹{entry:,.2f}</code>\n"
        f"🚪 Exit: <code>₹{exit_price:,.2f}</code>\n"
        f"{pnl_emoji} PnL: <code>₹{pnl:+,.2f} ({pnl_pct:+.2f}%)</code>\n"
        f"📋 Reason: <b>{display_reason}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now}"
    )

    return _send_message(text)


def send_daily_summary(stats: dict) -> bool:
    """Send end-of-day performance summary."""
    now = datetime.now().strftime("%d %b %Y")

    text = (
        f"📊 <b>Daily Summary — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Trades: <b>{stats.get('total_trades', 0)}</b>\n"
        f"✅ Win Rate: <b>{stats.get('win_rate', 0):.1f}%</b>\n"
        f"💰 Day PnL: <code>₹{stats.get('daily_pnl', 0):+,.2f}</code>\n"
        f"🏦 Capital: <code>₹{stats.get('capital', 0):,.2f}</code>\n"
        f"📉 Max DD: <b>{stats.get('max_drawdown_pct', 0):.1f}%</b>\n"
        f"📊 Sharpe: <b>{stats.get('sharpe_ratio', 0):.2f}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 TradeAI PRO"
    )

    return _send_message(text)


def send_weekly_digest(weekly_stats: list[dict]) -> bool:
    """Send weekly performance digest — list of daily stats."""
    now = datetime.now().strftime("%d %b %Y")

    total_pnl = sum(s.get("daily_pnl", 0) for s in weekly_stats)
    total_trades = sum(s.get("total_trades", 0) for s in weekly_stats)
    avg_win_rate = (
        sum(s.get("win_rate", 0) for s in weekly_stats) / len(weekly_stats)
        if weekly_stats else 0
    )

    pnl_emoji = "📈" if total_pnl >= 0 else "📉"

    text = (
        f"📅 <b>Weekly Digest — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔢 Total Trades: <b>{total_trades}</b>\n"
        f"✅ Avg Win Rate: <b>{avg_win_rate:.1f}%</b>\n"
        f"{pnl_emoji} Week PnL: <code>₹{total_pnl:+,.2f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 TradeAI PRO — Week in Review"
    )

    return _send_message(text)


def send_regime_change_alert(symbol: str, old_regime: str, new_regime: str) -> bool:
    """Alert when market regime changes for a symbol."""
    regime_emoji = {
        "TRENDING": "🚀",
        "RANGING": "📦",
        "HIGH_VOLATILITY": "⚡",
    }
    emoji = regime_emoji.get(new_regime, "🔄")

    text = (
        f"{emoji} <b>Regime Change — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📤 Was: <b>{old_regime}</b>\n"
        f"📥 Now: <b>{new_regime}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )

    return _send_message(text)


def send_health_alert(message: str, level: str = "INFO") -> bool:
    """Send a health/error notification."""
    emoji = {"INFO": "ℹ️", "WARNING": "⚠️", "ERROR": "🚨", "RECOVERY": "✅"}.get(level, "ℹ️")

    text = (
        f"{emoji} <b>System Alert</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{message}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )

    return _send_message(text)


def send_data_feed_alert(symbol: str, source: str, error: str) -> bool:
    """Alert when a data feed fails (Binance, NSE, etc.)."""
    text = (
        f"📡 <b>Data Feed Failure</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Symbol: <b>{symbol}</b>\n"
        f"🔌 Source: <b>{source}</b>\n"
        f"❌ Error: <code>{error[:200]}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )
    return _send_message(text)


def send_stale_data_alert(symbol: str, staleness_seconds: float, expected_seconds: float) -> bool:
    """Alert when data is stale (no update received within expected interval)."""
    text = (
        f"⏳ <b>Stale Data Warning</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 Symbol: <b>{symbol}</b>\n"
        f"⏱️ Last update: <b>{staleness_seconds:.0f}s ago</b>\n"
        f"⚠️ Expected interval: <b>{expected_seconds:.0f}s</b>\n"
        f"🔄 Signal generation paused for this symbol\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )
    return _send_message(text)


def send_websocket_alert(status: str, reconnect_count: int = 0) -> bool:
    """Alert on WebSocket connection events."""
    if status == "disconnected":
        text = (
            f"🔌 <b>WebSocket Disconnected</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reconnection attempt #{reconnect_count}\n"
            f"Switching to CCXT polling fallback\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
        )
    elif status == "reconnected":
        text = (
            f"✅ <b>WebSocket Reconnected</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Real-time streaming resumed\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
        )
    else:
        return False
    return _send_message(text)


def send_forex_signal_alert(setup: dict) -> bool:
    """
    Send a forex swing signal alert in the standard format (F8 spec).

    Args:
        setup: ForexSetup.to_dict() payload

    Returns:
        True if message sent successfully, False otherwise.
    """
    symbol      = setup.get("symbol", "—")
    direction   = setup.get("direction", "—")
    session     = setup.get("session_name", "—")
    sess_active = setup.get("session_active", False)
    entry       = setup.get("entry", 0)
    sl          = setup.get("sl", 0)
    tp1         = setup.get("tp1", 0)
    tp2         = setup.get("tp2", 0)
    sl_pips     = setup.get("sl_pips", 0)
    rr          = setup.get("rr_ratio", 0)
    strength    = setup.get("strength_score", 0)
    pattern     = setup.get("pattern", "—").replace("_", " ").title()
    daily_trend = setup.get("daily_trend", "—")
    hold_min    = setup.get("hold_hours_min", 0)
    hold_max    = setup.get("hold_hours_max", 0)

    dir_arrow   = "↑" if direction == "LONG" else "↓"
    sess_dot    = "🟢" if sess_active else "🔴"
    hold_days   = f"{hold_min // 24}–{hold_max // 24} days" if hold_min >= 24 else f"{hold_min}–{hold_max}h"
    trend_icon  = "✅" if daily_trend == "BULLISH" else "❌"

    tp1_pips = round(abs(tp1 - entry) / setup.get("sl_pips", 1) * sl_pips, 0) if sl_pips else 0
    tp2_pips = round(abs(tp2 - entry) / setup.get("sl_pips", 1) * sl_pips, 0) if sl_pips else 0

    text = (
        f"💱 <b>FOREX SIGNAL — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Direction  : <b>{direction} {dir_arrow}</b>\n"
        f"Session    : {sess_dot} {session} {'ACTIVE' if sess_active else 'CLOSED'}\n"
        f"Entry      : <b>{entry}</b>\n"
        f"Stop Loss  : <b>{sl}</b> ({sl_pips:.0f} pips)\n"
        f"Target 1   : <b>{tp1}</b> ({tp1_pips:.0f} pips)\n"
        f"Target 2   : <b>{tp2}</b> ({tp2_pips:.0f} pips)\n"
        f"R:R Ratio  : 1:{rr}\n"
        f"Strength   : {strength}/100\n"
        f"Pattern    : {pattern}\n"
        f"Hold       : {hold_days}\n"
        f"Trend      : Daily {daily_trend} {trend_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Paper trade only. Not financial advice.\n"
        f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )
    return _send_message(text)


def send_consecutive_loss_alert(count: int, paused: bool = True) -> bool:
    """Alert when consecutive loss limit is approached or hit."""
    if paused:
        text = (
            f"🛑 <b>Trading Paused — Loss Limit</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌ {count} consecutive losses\n"
            f"Trading paused until next day\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
        )
    else:
        text = (
            f"⚠️ <b>Loss Warning — {count} Consecutive</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Approaching pause threshold ({settings.MAX_CONSECUTIVE_LOSSES})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
        )
    return _send_message(text)


def send_swing_shortlist_alert(
    shortlist: list[dict],
    scan_meta: dict | None = None,
    max_setups: int = 7,
) -> bool:
    """
    Send SwingSpectrum morning shortlist as a formatted Telegram message.

    Args:
        shortlist: List of ranked setup dicts from nightly_swing_scan output.
        scan_meta: Optional dict with scan_date, symbols_scanned, grade_summary, news_enriched.
        max_setups: Max number of setups to include (Telegram has 4096 char limit).

    Returns:
        True if sent successfully.
    """
    if not shortlist:
        return _send_message(
            "📋 <b>SwingSpectrum Morning Shortlist</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ Aaj koi qualifying setup nahi mila.\n"
            "Possible: market choppy, or all setups failed R:R / regime gate.\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
        )

    meta = scan_meta or {}
    scan_date = meta.get("scan_date", datetime.now().strftime("%Y-%m-%d"))
    scanned = meta.get("symbols_scanned", "?")
    grade_summary = meta.get("grade_summary", {})
    news_on = "✅" if meta.get("news_enriched") else "❌"

    # Header
    lines = [
        f"📋 <b>SwingSpectrum Morning Shortlist</b>",
        f"📅 {scan_date}  |  {scanned} symbols scanned",
        f"Grades: A:{grade_summary.get('A', 0)}  B:{grade_summary.get('B', 0)}  C:{grade_summary.get('C', 0)}  |  News: {news_on}",
        "━━━━━━━━━━━━━━━━━━━━━",
    ]

    shown = shortlist[:max_setups]
    for i, s in enumerate(shown, 1):
        grade = s.get("grade", "C")
        direction = s.get("direction", "")
        symbol = s.get("symbol", "")
        pattern = s.get("pattern", "")
        entry = s.get("entry") or s.get("entry_zone", 0)
        sl = s.get("sl") or s.get("stop_loss", 0)
        t1 = s.get("t1") or s.get("target_1", 0)
        rr = s.get("rr") or s.get("risk_reward", 0)
        score = s.get("composite_score", 0)
        ns = float(s.get("news_score", 0) or 0)

        grade_icon = {"A": "🟢", "B": "🟡", "C": "⚪"}.get(grade, "⚪")
        dir_icon = "▲" if direction == "LONG" else "▼"
        news_tag = ""
        if ns > 0.1:
            news_tag = " 📰+"
        elif ns < -0.1:
            news_tag = " 📰−"

        lines.append(
            f"\n{i}. {grade_icon}<b>{grade}</b> {dir_icon} <b>{symbol}</b>{news_tag}"
        )
        lines.append(f"   {pattern}")
        lines.append(
            f"   Entry: <code>₹{float(entry):,.2f}</code>  "
            f"SL: <code>₹{float(sl):,.2f}</code>  "
            f"T1: <code>₹{float(t1):,.2f}</code>"
        )
        lines.append(
            f"   R:R <b>{float(rr):.1f}x</b>  |  Score <b>{float(score):.0f}</b>/100"
        )

        # Entry note (truncated)
        note = s.get("entry_note", "")
        if note:
            lines.append(f"   💡 {note[:80]}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━")

    if len(shortlist) > max_setups:
        lines.append(f"... +{len(shortlist) - max_setups} more setups in full report")

    lines.append("⚠️ Paper only. Not financial advice. Always use SL.")
    lines.append(f"⏰ {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}")

    text = "\n".join(lines)

    # Telegram limit: 4096 chars. Truncate gracefully if needed.
    if len(text) > 4000:
        text = text[:3950] + "\n\n... (truncated — see full report on dashboard)"

    return _send_message(text)

