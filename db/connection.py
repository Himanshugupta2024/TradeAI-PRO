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
    kwargs = {"echo": False}
    if "sqlite" in DATABASE_URL:
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    eng = create_engine(DATABASE_URL, **kwargs)
    # Enable WAL mode for SQLite — allows concurrent reads
    if "sqlite" in DATABASE_URL:
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
