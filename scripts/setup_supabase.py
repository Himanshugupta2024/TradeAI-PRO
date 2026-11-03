"""
TradeAI-PRO — Supabase PostgreSQL Schema Setup & Verification
Safely provisions isolated 'tradeai' schema in Supabase PostgreSQL
without touching any existing tables in 'public' schema.
"""

import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("supabase_setup")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import create_engine, text
from db.models import Base

def setup_supabase_schema(database_url: str):
    if not any(database_url.startswith(p) for p in ("postgresql://", "postgres://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        log.error("DATABASE_URL must be a PostgreSQL connection string (Supabase). Got: %s", database_url)
        return False

    url = database_url
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    # Normalize to psycopg or psycopg2
    try:
        import psycopg
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    except ImportError:
        pass

    log.info("Connecting to Supabase PostgreSQL...")
    engine = create_engine(url, pool_pre_ping=True)

    with engine.connect() as conn:
        log.info("Ensuring isolated schema 'tradeai' exists...")
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS tradeai;"))
        conn.commit()
        log.info("[OK] Schema 'tradeai' verified.")

        log.info("Setting search_path to 'tradeai,public'...")
        conn.execute(text("SET search_path TO tradeai, public;"))

        log.info("Creating TradeAI tables inside 'tradeai' schema...")
        Base.metadata.create_all(bind=conn)
        conn.commit()
        log.info("[OK] All TradeAI tables successfully created in 'tradeai' schema.")

        # Verify tables created
        res = conn.execute(text("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = 'tradeai'
            ORDER BY table_name;
        """)).fetchall()
        tables = [r[0] for r in res]
        log.info("TradeAI tables in 'tradeai' schema: %s", tables)

    print("\n" + "=" * 80)
    print("SUPABASE SETUP COMPLETED SUCCESSFULLY!")
    print(f"Total Tables Created: {len(tables)}")
    print("Isolated Schema: tradeai (Existing 'public' tables untouched)")
    print("=" * 80)
    return True

if __name__ == "__main__":
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("Usage: Pass DATABASE_URL via environment variable, e.g.:")
        print("  DATABASE_URL='postgresql://postgres:[PASSWORD]@db.xxxx.supabase.co:5432/postgres' python scripts/setup_supabase.py")
    else:
        setup_supabase_schema(db_url)

