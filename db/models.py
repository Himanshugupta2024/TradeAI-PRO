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

    state_key = Column(String(500), primary_key=True)
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


# ── Swing Scanner Persistence ─────────────────────────────────────
# Stores each full-universe SwingSpectrum scan snapshot so the UI, stats,
# and outcome tracker survive process restarts. Paired with SwingSetupOutcome
# which rows emitted setups and fills SL/T1/T2 hit results at EOD.

class SwingScan(Base):
    __tablename__ = "swing_scans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_date = Column(Date, nullable=False, index=True)
    scan_time = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # How the scan was triggered: "manual" / "startup" / "periodic" / "cron".
    trigger = Column(String(20), nullable=False, default="manual")

    symbols_scanned = Column(Integer, nullable=False, default=0)
    total_setups = Column(Integer, nullable=False, default=0)
    grade_a_count = Column(Integer, nullable=False, default=0)
    grade_b_count = Column(Integer, nullable=False, default=0)
    grade_c_count = Column(Integer, nullable=False, default=0)

    # Full JSON payload (list[SwingSetup asdict]) for rehydrating the dashboard.
    # Stored as TEXT so SQLite and Postgres both work without JSON column type.
    payload_json = Column(Text, nullable=False, default="[]")
    # Market context dict used by the regime gate at scan time (optional).
    market_context_json = Column(Text, nullable=True)

    outcomes = relationship(
        "SwingSetupOutcome",
        back_populates="scan",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_swing_scans_date_time", "scan_date", "scan_time"),
    )

    def __repr__(self):
        return (
            f"<SwingScan {self.scan_date} setups={self.total_setups} "
            f"A={self.grade_a_count} B={self.grade_b_count} C={self.grade_c_count}>"
        )


class SwingSetupOutcome(Base):
    """Per-setup outcome row — filled lazily by the EOD outcome checker.

    status values:
      OPEN      — setup still within timeframe window, no level hit
      T1_HIT    — target_1 touched/exceeded (partial win)
      T2_HIT    — target_2 touched/exceeded (full win)
      SL_HIT    — stop_loss breached (loss)
      EXPIRED   — timeframe window elapsed without T1 or SL (neutral)
    """
    __tablename__ = "swing_setup_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(Integer, ForeignKey("swing_scans.id"), nullable=False, index=True)

    symbol = Column(String(30), nullable=False, index=True)
    pattern = Column(String(80), nullable=False)
    direction = Column(String(10), nullable=False)   # LONG / SHORT
    grade = Column(String(2), nullable=False, default="C")
    strength = Column(Integer, nullable=False, default=0)

    entry_zone = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    target_1 = Column(Float, nullable=False)
    target_2 = Column(Float, nullable=False)
    risk_reward = Column(Float, nullable=False, default=0.0)
    timeframe = Column(String(20), nullable=True)      # e.g. "3-7 days"

    emitted_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    # Outcome tracking — filled by EOD checker.
    status = Column(String(20), nullable=False, default="OPEN", index=True)
    hit_level = Column(String(20), nullable=True)        # t1 / t2 / sl / expired / missed_entry
    hit_price = Column(Float, nullable=True)
    hit_at = Column(DateTime, nullable=True)
    bars_to_hit = Column(Integer, nullable=True)
    pnl_pct = Column(Float, nullable=True)               # signed % vs entry

    scan = relationship("SwingScan", back_populates="outcomes")

    __table_args__ = (
        Index("idx_swing_outcome_status", "status", "symbol"),
        Index("idx_swing_outcome_grade", "grade", "status"),
    )

    def __repr__(self):
        return (
            f"<SwingSetupOutcome {self.symbol} {self.direction} grade={self.grade} "
            f"status={self.status}>"
        )


# ── Intraday scanner persistence (B-lite, paper-only) ─────────────

class IntradayScan(Base):
    """
    Intraday scan snapshot with full Freshness Contract metadata.
    Supports both manual API triggers and automated scheduled workers.
    """

    __tablename__ = "intraday_scans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_id = Column(String(64), nullable=True, index=True)
    scan_time = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    source_last_candle_at = Column(DateTime, nullable=True)
    market_session_date = Column(Date, nullable=True)
    provider = Column(String(32), nullable=True, default="yfinance")
    provider_freshness = Column(String(32), nullable=True, default="15m Delayed")
    symbols_attempted = Column(Integer, nullable=True, default=0)
    symbols_successful = Column(Integer, nullable=True, default=0)
    symbols_failed = Column(Integer, nullable=True, default=0)
    scanner_version = Column(String(16), nullable=True, default="2.0.0")
    scan_duration_ms = Column(Integer, nullable=True, default=0)
    trigger = Column(String(24), nullable=False, default="manual")
    symbols_scanned = Column(Integer, nullable=False, default=0)
    signal_count = Column(Integer, nullable=False, default=0)
    payload_json = Column(Text, nullable=False, default="[]")
    meta_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_intraday_scans_time", "scan_time"),
        Index("idx_intraday_scans_scan_id", "scan_id"),
    )

    def __repr__(self):
        return f"<IntradayScan id={self.id} scan_id={self.scan_id} signals={self.signal_count}>"


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



# ── Distributed Worker Lock ──────────────────────────────────────────
class WorkerDistributedLock(Base):
    """
    Database-backed distributed lock table for coordinating worker runs across
    GitHub Actions, cron schedules, local runners, and container instances.
    """
    __tablename__ = "worker_distributed_locks"

    job_name = Column(String(64), primary_key=True)
    run_id = Column(String(64), nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    locked_until = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="RUNNING")  # RUNNING, COMPLETED, FAILED, EXPIRED
    heartbeat_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    meta_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_worker_lock_status", "status", "locked_until"),
    )

    def __repr__(self):
        return f"<WorkerDistributedLock job={self.job_name} run_id={self.run_id} status={self.status}>"


# ── Autonomous Decision Record ───────────────────────────────────────
class AutonomousDecisionRecord(Base):
    """
    Persistent audit log of autonomous paper trading decisions.
    Enforces deterministic signal idempotency via unique signal_id constraint.
    """
    __tablename__ = "autonomous_decision_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String(128), nullable=False, unique=True, index=True)
    run_id = Column(String(64), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    action = Column(String(32), nullable=False, index=True)  # AUTO_ENTER, REJECT, SKIP, NO_TRADE, EXIT, TRAILING_STOP
    direction = Column(String(10), nullable=True)            # LONG, SHORT, HOLD
    entry_price = Column(Float, nullable=True)
    stop_loss = Column(Float, nullable=True)
    target_price = Column(Float, nullable=True)
    quality_score = Column(Float, nullable=True)
    confluence_score = Column(Float, nullable=True)
    risk_reward = Column(Float, nullable=True)
    market_regime = Column(String(32), nullable=True)
    source_freshness = Column(String(20), nullable=True)    # LIVE, DELAYED, STALE, EOD, UNAVAILABLE
    paper_trade_id = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    meta_json = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_auto_decision_symbol_action", "symbol", "action"),
        Index("idx_auto_decision_run_id", "run_id"),
    )

    def __repr__(self):
        return f"<AutonomousDecisionRecord {self.symbol} {self.action} run={self.run_id}>"


# ── Trade Outcome (Persistent Learning Pipeline) ────────────────────────────
class TradeOutcome(Base):
    """
    Persistent trade outcome record for the self-learning feedback loop.
    Migrated from ephemeral SQLite trade_outcomes to PostgreSQL for
    Render restart safety.

    One row per closed paper trade. Used by FeedbackAnalyser to compute
    signal-type win rates and by WeightAdapter to update learned weights.

    Unique constraint on trade_id prevents duplicate learning outcomes
    from the same trade close event.
    """
    __tablename__ = "trade_outcomes_pg"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(String(128), nullable=True, unique=True, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    signal_type = Column(String(30), nullable=False, default="COMBINED")
    regime = Column(String(30), nullable=False, default="UNKNOWN")
    rsi_at_entry = Column(Float, nullable=True)
    volume_above_avg = Column(Integer, nullable=False, default=0)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    result = Column(String(10), nullable=False)  # WIN / LOSS
    closed_at = Column(String(64), nullable=False)
    confidence = Column(Float, default=0.0)
    exit_reason = Column(String(30), default="")
    # Enriched metadata for future analysis
    asset_class = Column(String(20), nullable=True)  # EQUITY / CRYPTO / FOREX
    strategy_version = Column(String(20), nullable=True)
    environment = Column(String(30), nullable=False, default="PAPER_FORWARD")
    entry_time = Column(String(64), nullable=True)
    exit_time = Column(String(64), nullable=True)
    stop_loss = Column(Float, nullable=True)
    target = Column(Float, nullable=True)
    pnl = Column(Float, nullable=True)
    quantity = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_trade_outcomes_pg_signal_type", "signal_type"),
        Index("idx_trade_outcomes_pg_regime", "regime"),
        Index("idx_trade_outcomes_pg_result", "result"),
        Index("idx_trade_outcomes_pg_env", "environment"),
    )

    def __repr__(self):
        return (
            f"<TradeOutcome {self.symbol} {self.direction} "
            f"{self.result} pnl={self.pnl_pct}%>"
        )

