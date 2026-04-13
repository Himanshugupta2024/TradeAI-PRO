"""
SQLAlchemy models for the Trading System.
Tables: Candle, Feature, Signal, Trade
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Date, Enum, ForeignKey,
    UniqueConstraint, Text,
    Index,
)
from sqlalchemy.orm import relationship, DeclarativeBase
import enum


class Base(DeclarativeBase):
    pass


# ── Enums ────────────────────────────────────────────────

class Direction(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    HOLD = "HOLD"
    # Legacy aliases kept for backward compatibility with stored records
    BUY = "BUY"
    SELL = "SELL"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PAPER = "PAPER"


class TrendDirection(str, enum.Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# ── Candle ───────────────────────────────────────────────

class Candle(Base):
    __tablename__ = "candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(5), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False, default=0.0)

    # One candle → one feature row
    feature = relationship("Feature", back_populates="candle", uselist=False)

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle"),
        # Composite index for efficient time-range queries (e.g. get last N bars)
        # Priority: symbol lookup is most common, then timeframe, then temporal range
    )

    def __repr__(self):
        return f"<Candle {self.symbol} {self.timeframe} {self.timestamp}>"


# ── Feature ──────────────────────────────────────────────

class Feature(Base):
    __tablename__ = "features"

    id = Column(Integer, primary_key=True, autoincrement=True)
    candle_id = Column(Integer, ForeignKey("candles.id"), nullable=False, unique=True)

    # Technical Indicators
    rsi = Column(Float)
    macd = Column(Float)
    macd_signal = Column(Float)
    macd_hist = Column(Float)
    ema_20 = Column(Float)
    ema_50 = Column(Float)
    ema_200 = Column(Float)
    atr = Column(Float)

    # Support / Resistance
    support = Column(Float)
    resistance = Column(Float)

    # Higher timeframe trend
    htf_trend = Column(Enum(TrendDirection))

    candle = relationship("Candle", back_populates="feature")

    def __repr__(self):
        return f"<Feature candle_id={self.candle_id} rsi={self.rsi}>"


# ── Signal ───────────────────────────────────────────────

class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    timeframe = Column(String(5), nullable=False)
    direction = Column(Enum(Direction), nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    target_price = Column(Float)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    trades = relationship("Trade", back_populates="signal")

    def __repr__(self):
        return f"<Signal {self.direction.value} {self.symbol} conf={self.confidence:.0f}%>"


# ── Trade ────────────────────────────────────────────────

class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, ForeignKey("signals.id"), nullable=False)
    fill_price = Column(Float)
    exit_price = Column(Float)
    quantity = Column(Float, default=1.0)
    pnl = Column(Float, default=0.0)
    pnl_pct = Column(Float, default=0.0)
    status = Column(Enum(TradeStatus), nullable=False, default=TradeStatus.PAPER)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)

    # Trailing stop-loss persistence
    trailing_sl = Column(Float, nullable=True)
    trailing_highest = Column(Float, nullable=True)
    trailing_breakeven = Column(Integer, default=0)  # 0=False, 1=True

    signal = relationship("Signal", back_populates="trades")

    def __repr__(self):
        return f"<Trade #{self.id} {self.status.value} pnl={self.pnl}>"


# ── Morning Scan ────────────────────────────────────────

class MorningScan(Base):
    __tablename__ = "morning_scans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_time = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    scan_date = Column(Date, nullable=False, index=True)
    rank = Column(Integer, nullable=False, default=0)

    symbol = Column(String(30), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    confidence_score = Column(Float, nullable=False, default=0.0, index=True)
    reason = Column(String(255), nullable=False)

    # Snapshot values for transparency and auditability.
    close = Column(Float, nullable=False)
    rsi = Column(Float, nullable=False)
    macd = Column(Float, nullable=False)
    macd_signal = Column(Float, nullable=False)
    ema_20 = Column(Float, nullable=False)
    ema_50 = Column(Float, nullable=False)
    ema_200 = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    avg_volume_20 = Column(Float, nullable=False)
    volume_ratio = Column(Float, nullable=False)

    __table_args__ = (
        Index("ix_morning_scans_scan_rank", "scan_time", "confidence_score"),
        Index("ix_morning_scans_date_symbol", "scan_date", "symbol"),
    )

    def __repr__(self):
        return (
            f"<MorningScan {self.symbol} score={self.confidence_score:.1f} "
            f"entry={self.entry_price:.2f}>"
        )


# ── OHLCV Cache ────────────────────────────────────────

class OhlcvCache(Base):
    __tablename__ = "ohlcv_cache"

    symbol = Column(String(30), primary_key=True)
    timeframe = Column(String(5), primary_key=True)
    timestamp = Column(DateTime, primary_key=True)
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    source = Column(String(30), nullable=False, default="yfinance")
    fetched_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_ohlcv_sym_tf", "symbol", "timeframe", "timestamp"),
    )


# ── Model Performance ───────────────────────────────────

class ModelPerformance(Base):
    __tablename__ = "model_performance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_name = Column(String(30), nullable=False, index=True)
    predicted = Column(String(10), nullable=False)
    actual = Column(String(10), nullable=False)
    symbol = Column(String(30), nullable=True, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("idx_model_perf_model_ts", "model_name", "timestamp"),
    )


# ── Context Snapshots ──────────────────────────────────

class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(30), nullable=False, index=True)
    timestamp_utc = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    # 24-hour context
    price_change_24hr_pct = Column(Float, nullable=False, default=0.0)
    volume_change_24hr_pct = Column(Float, nullable=False, default=0.0)
    news_sentiment_24hr = Column(Float, nullable=False, default=0.0)
    news_volume_spike = Column(Integer, nullable=False, default=0)

    # 7-day context
    weekly_trend_bias = Column(String(20), nullable=False, default="NEUTRAL")
    oi_change_7d_pct = Column(Float, nullable=False, default=0.0)
    btc_dominance_7d_trend = Column(String(20), nullable=False, default="FLAT")

    # Event risk
    days_to_next_event = Column(Integer, nullable=False, default=99)
    event_risk_active = Column(Integer, nullable=False, default=0)

    # Composite score
    context_score = Column(Float, nullable=False, default=0.0)

    __table_args__ = (
        Index("idx_context_snapshots_symbol_ts", "symbol", "timestamp_utc"),
    )


# ── Runtime Key-Value State ───────────────────────────

class RuntimeState(Base):
    __tablename__ = "runtime_state"

    state_key = Column(String(120), primary_key=True)
    state_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


# ── Signal Log (Win Rate Tracker) ────────────────────────────────
# Logs every BUY/SELL/HOLD signal emitted. A background job fills
# price_24h_later + outcome after 24h to compute per-coin win rates.

class SignalLog(Base):
    __tablename__ = "signal_log"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String(30), nullable=False, index=True)
    signal          = Column(String(10), nullable=False)            # BUY / SELL / HOLD
    confidence      = Column(Float, nullable=False, default=0.0)
    price_at_signal = Column(Float, nullable=False)
    rsi_at_signal   = Column(Float, nullable=True)
    context_score   = Column(Float, nullable=True)
    emitted_at      = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    # Filled by background scorer 24h later
    price_24h_later = Column(Float, nullable=True)
    outcome         = Column(String(10), nullable=True)   # WIN / LOSS / NEUTRAL
    outcome_pct     = Column(Float, nullable=True)        # % move after signal
    scored_at       = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_signal_log_symbol_ts", "symbol", "emitted_at"),
        Index("idx_signal_log_outcome",   "symbol", "outcome"),
    )

    def __repr__(self):
        return (
            f"<SignalLog {self.signal} {self.symbol} "
            f"@ {self.price_at_signal} outcome={self.outcome}>"
        )


# ── Telegram Alert Log ────────────────────────────────────────────
# Prevents duplicate alerts from being sent for the same signal event.

class TelegramAlertLog(Base):
    __tablename__ = "telegram_alert_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    symbol      = Column(String(30), nullable=False, index=True)
    signal      = Column(String(10), nullable=False)
    sent_at     = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    message     = Column(Text, nullable=True)
    delivered   = Column(Integer, nullable=False, default=1)   # 1=ok, 0=failed

    __table_args__ = (
        Index("idx_tg_alert_symbol_ts", "symbol", "sent_at"),
    )
