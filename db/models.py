"""
SQLAlchemy models for the Trading System.
Tables: Candle, Feature, Signal, Trade
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, Float, String, DateTime, Date, Enum, ForeignKey,
    UniqueConstraint,
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
