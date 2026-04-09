"""add_cache_and_model_performance

Revision ID: 7f9b2a0a6d1c
Revises: 50678bd8b636
Create Date: 2026-04-07 00:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7f9b2a0a6d1c"
down_revision: Union[str, Sequence[str], None] = "50678bd8b636"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ohlcv_cache",
        sa.Column("symbol", sa.String(length=30), nullable=False),
        sa.Column("timeframe", sa.String(length=5), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("open", sa.Float(), nullable=True),
        sa.Column("high", sa.Float(), nullable=True),
        sa.Column("low", sa.Float(), nullable=True),
        sa.Column("close", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="yfinance"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "timestamp"),
    )
    op.create_index("idx_ohlcv_sym_tf", "ohlcv_cache", ["symbol", "timeframe", "timestamp"], unique=False)

    op.create_table(
        "model_performance",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("model_name", sa.String(length=30), nullable=False),
        sa.Column("predicted", sa.String(length=10), nullable=False),
        sa.Column("actual", sa.String(length=10), nullable=False),
        sa.Column("symbol", sa.String(length=30), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_performance_model_name", "model_performance", ["model_name"], unique=False)
    op.create_index("ix_model_performance_symbol", "model_performance", ["symbol"], unique=False)
    op.create_index("ix_model_performance_timestamp", "model_performance", ["timestamp"], unique=False)
    op.create_index("idx_model_perf_model_ts", "model_performance", ["model_name", "timestamp"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_model_perf_model_ts", table_name="model_performance")
    op.drop_index("ix_model_performance_timestamp", table_name="model_performance")
    op.drop_index("ix_model_performance_symbol", table_name="model_performance")
    op.drop_index("ix_model_performance_model_name", table_name="model_performance")
    op.drop_table("model_performance")

    op.drop_index("idx_ohlcv_sym_tf", table_name="ohlcv_cache")
    op.drop_table("ohlcv_cache")
