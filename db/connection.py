"""
Database connection and session management.
Auto-creates all tables on import.
"""

import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from config.settings import DATABASE_URL, IS_PRODUCTION
from db.models import Base

logger = logging.getLogger(__name__)


def _create_engine():
    """Create the SQLAlchemy engine with appropriate settings."""
    url = DATABASE_URL or "sqlite:///trading.db"
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    # Normalize bare postgresql:// URLs to an installed driver (psycopg3 or psycopg2)
    if url.startswith("postgresql://"):
        try:
            import psycopg
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        except ImportError:
            try:
                import psycopg2
                url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
            except ImportError:
                pass

    kwargs = {"echo": False}
    if "sqlite" in url:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 10
        kwargs["max_overflow"] = 20
        # Disable server-side prepared statements for Supabase PgBouncer / Transaction pooler
        if "psycopg" in url and "psycopg2" not in url:
            kwargs["connect_args"] = {"prepare_threshold": None, "connect_timeout": 5}
        else:
            kwargs["connect_args"] = {"connect_timeout": 5}

    eng = create_engine(url, **kwargs)
    # Enable WAL mode for SQLite - allows concurrent reads
    if "sqlite" in url:
        from sqlalchemy import event
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
    return eng


engine = _create_engine()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)



def _migrate_schema():
    """Add any missing columns to existing tables for backwards compatibility."""
    try:
        with engine.connect() as conn:
            # Check intraday_scans columns
            cols_to_add = [
                ("scan_id", "VARCHAR(64)"),
                ("started_at", "TIMESTAMP"),
                ("completed_at", "TIMESTAMP"),
                ("source_last_candle_at", "TIMESTAMP"),
                ("market_session_date", "DATE"),
                ("provider", "VARCHAR(32) DEFAULT 'yfinance'"),
                ("provider_freshness", "VARCHAR(32) DEFAULT '15m Delayed'"),
                ("symbols_attempted", "INTEGER DEFAULT 0"),
                ("symbols_successful", "INTEGER DEFAULT 0"),
                ("symbols_failed", "INTEGER DEFAULT 0"),
                ("scanner_version", "VARCHAR(16) DEFAULT '2.0.0'"),
                ("scan_duration_ms", "INTEGER DEFAULT 0"),
                ("meta_json", "TEXT"),
            ]
            
            # Check existing columns
            existing = set()
            try:
                if "sqlite" in str(engine.url):
                    res = conn.execute(text("PRAGMA table_info(intraday_scans)")).fetchall()
                    existing = {r[1] for r in res}
                else:
                    res = conn.execute(text(
                        "SELECT column_name FROM information_schema.columns WHERE table_name = 'intraday_scans'"
                    )).fetchall()
                    existing = {r[0] for r in res}
            except Exception:
                pass

            for col_name, col_type in cols_to_add:
                if existing and col_name not in existing:
                    try:
                        conn.execute(text(f"ALTER TABLE intraday_scans ADD COLUMN {col_name} {col_type}"))
                        conn.commit()
                        logger.info("Migrated intraday_scans: added column %s", col_name)
                    except Exception as e:
                        logger.debug("Could not add column %s: %s", col_name, e)
    except Exception as exc:
        logger.debug("Schema migration check skipped: %s", exc)


def init_db():
    """Create all tables if they don't exist and run auto-migrations."""
    try:
        Base.metadata.create_all(bind=engine)
        _migrate_schema()
    except Exception as exc:
        logger.exception("Failed to initialize database schema: %s", exc)
        if IS_PRODUCTION:
            raise RuntimeError(f"Database schema initialization failed in production: {exc}") from exc
        raise


def check_db_connectivity() -> bool:
    """Check if the database is reachable and accepting queries."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.debug("Database connectivity check failed: %s", exc)
        return False


def get_session():
    """Yield a new DB session (use in `with` or as context manager)."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_sqlite_path() -> str:
    """
    Return the local filesystem path for SQLite connections.
    Extracts the path from settings.DATABASE_URL (e.g. sqlite:////tmp/trading.db or sqlite:///trading.db).
    """
    url = DATABASE_URL or "sqlite:///trading.db"
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "")
    return "trading.db"
