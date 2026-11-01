"""
Database connection and session management.
Auto-creates all tables on import.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config.settings import DATABASE_URL
from db.models import Base


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
            kwargs["connect_args"] = {"prepare_threshold": None}

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


def init_db():
    """Create all tables if they don't exist."""
    Base.metadata.create_all(bind=engine)


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
