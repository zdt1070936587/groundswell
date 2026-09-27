"""Phase 5: idea lifecycle, subscriptions, compression policies

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMPRESSION = {
    "item_snapshots": ("item_id", "14 days"),
    "keyword_hourly_stats": ("key, region", "30 days"),
    "topic_hourly_stats": ("topic_id, region", "30 days"),
}


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "ideas",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=True),
        sa.Column("topic_id", sa.BigInteger(), nullable=True),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("opportunity_id"),
    )
    op.create_index("ix_ideas_status", "ideas", ["status"], unique=False)

    op.create_table(
        "idea_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("idea_id", sa.BigInteger(), nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("user_name", sa.String(length=64), nullable=False),
        _now("ts"),
        sa.ForeignKeyConstraint(["idea_id"], ["ideas.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_idea_events_idea", "idea_events", ["idea_id"], unique=False)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("keywords", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("regions", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("categories", postgresql.ARRAY(sa.String()), nullable=False),
        sa.Column("min_score", sa.Float(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _now("created_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "subscription_hits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("subscription_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        _now("ts"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subscription_id", "topic_id", "region", name="uq_subscription_hits"),
    )

    for table, (segment_by, after) in COMPRESSION.items():
        op.execute(
            f"ALTER TABLE {table} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby = '{segment_by}')"
        )
        op.execute(
            f"SELECT add_compression_policy('{table}', INTERVAL '{after}', if_not_exists => true)"
        )


def downgrade() -> None:
    for table in COMPRESSION:
        op.execute(f"SELECT remove_compression_policy('{table}', if_exists => true)")
    op.drop_table("subscription_hits")
    op.drop_table("subscriptions")
    op.drop_index("ix_idea_events_idea", table_name="idea_events")
    op.drop_table("idea_events")
    op.drop_index("ix_ideas_status", table_name="ideas")
    op.drop_table("ideas")
