"""v1 schema: collection, topics, opportunities (region built in)

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HYPERTABLES = {
    "item_snapshots": ("ts", "7 days"),
    "keyword_hourly_stats": ("hour", "7 days"),
    "topic_hourly_stats": ("hour", "7 days"),
}


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False)


def _now(name: str) -> sa.Column:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _jsonb(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=nullable)


def _array(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, postgresql.ARRAY(sa.String()), nullable=nullable)


def _ts(name: str, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "alerts",
        _id(),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        _now("ts"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_alerts_kind_key_ts", "alerts", ["kind", "key", "ts"], unique=False)

    op.create_table(
        "keyword_hourly_stats",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        _ts("hour", nullable=False),
        sa.Column("mentions", sa.Float(), nullable=False),
        sa.Column("new_items", sa.Float(), nullable=False),
        sa.Column("sources_count", sa.Integer(), nullable=False),
        sa.Column("z", sa.Float(), nullable=True),
        sa.Column("growth", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("key", "region", "hour"),
    )

    op.create_table(
        "llm_usage",
        _id(),
        _now("ts"),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_usage_ts", "llm_usage", ["ts"], unique=False)

    op.create_table(
        "methodology",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("person", sa.String(length=64), nullable=False),
        sa.Column("principle", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("tag", sa.String(length=64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("region_specific", sa.Boolean(), nullable=False),
        _array("signals"),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("default_region", sa.String(length=16), nullable=False),
        sa.Column("default_country", sa.String(length=8), nullable=True),
        sa.Column("default_language", sa.String(length=16), nullable=True),
        _jsonb("config"),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        _ts("last_run_at"),
        _ts("last_success_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("last_item_count", sa.Integer(), nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "topic_hourly_stats",
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        _ts("hour", nullable=False),
        sa.Column("mentions", sa.Float(), nullable=False),
        sa.Column("new_items", sa.Float(), nullable=False),
        sa.Column("engagement", sa.Float(), nullable=False),
        sa.Column("sources_count", sa.Integer(), nullable=False),
        sa.Column("z", sa.Float(), nullable=True),
        sa.Column("growth", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("topic_id", "region", "hour"),
    )

    op.create_table(
        "topics",
        _id(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=16), nullable=True),
        sa.Column("category_confidence", sa.Float(), nullable=True),
        _array("keywords"),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("origin_region", sa.String(length=16), nullable=True),
        _array("active_regions"),
        _jsonb("spread_path", nullable=True),
        _array("slow_variables"),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("merged_into", sa.BigInteger(), nullable=True),
        _ts("first_seen", nullable=False),
        _ts("last_seen", nullable=False),
        _ts("named_at"),
        _ts("classified_at"),
        _now("created_at"),
        sa.ForeignKeyConstraint(["merged_into"], ["topics.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_topics_embedding_hnsw",
        "topics",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_topics_status_last_seen", "topics", ["status", "last_seen"], unique=False)

    op.create_table(
        "opportunities",
        _id(),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("input_hash", sa.String(length=40), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("methodology_version", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        _jsonb("scores"),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("rank_score", sa.Float(), nullable=True),
        _array("tags"),
        sa.Column("why_now", sa.Text(), nullable=True),
        sa.Column("distribution_path", sa.Text(), nullable=True),
        _jsonb("product_shapes"),
        _jsonb("counter_arguments"),
        _jsonb("known_players"),
        sa.Column("durability", sa.String(length=16), nullable=False),
        sa.Column("is_gap", sa.Boolean(), nullable=False),
        sa.Column("gap_reason", sa.String(length=32), nullable=True),
        _now("created_at"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_id", "region", "input_hash", name="uq_opportunities_input"),
    )
    op.create_index("ix_opportunities_created", "opportunities", ["created_at"], unique=False)

    op.create_table(
        "raw_items",
        _id(),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("channel", sa.String(length=128), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("author_hash", sa.String(length=32), nullable=True),
        _ts("published_at"),
        _ts("fetched_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        _jsonb("metrics"),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("country", sa.String(length=8), nullable=True),
        sa.Column("region", sa.String(length=16), nullable=True),
        sa.Column("region_confidence", sa.Float(), nullable=True),
        sa.Column("region_method", sa.String(length=16), nullable=True),
        _array("keywords", nullable=True),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("dup_of", sa.BigInteger(), nullable=True),
        _ts("processed_at"),
        sa.ForeignKeyConstraint(["dup_of"], ["raw_items.id"]),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external"),
    )
    op.create_index("ix_raw_items_fetched_at", "raw_items", ["fetched_at"], unique=False)
    op.create_index("ix_raw_items_region", "raw_items", ["region"], unique=False)
    op.create_index(
        "ix_raw_items_unprocessed",
        "raw_items",
        ["id"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    op.create_table(
        "source_runs",
        _id(),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        _ts("started_at", nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("new_items", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_runs_source_started", "source_runs", ["source_id", "started_at"], unique=False
    )

    op.create_table(
        "validations",
        _id(),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("signal", sa.Float(), nullable=True),
        _jsonb("payload"),
        _now("ts"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_validations_lookup", "validations", ["topic_id", "region", "kind", "ts"], unique=False
    )

    op.create_table(
        "evidence",
        _id(),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        _jsonb("value", nullable=True),
        sa.ForeignKeyConstraint(["item_id"], ["raw_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_evidence_opportunity", "evidence", ["opportunity_id"], unique=False)

    op.create_table(
        "feedback",
        _id(),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.String(length=8), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("user_name", sa.String(length=64), nullable=False),
        _now("ts"),
        sa.ForeignKeyConstraint(["opportunity_id"], ["opportunities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_opportunity", "feedback", ["opportunity_id"], unique=False)

    op.create_table(
        "item_embeddings",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(["item_id"], ["raw_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id"),
    )

    op.create_table(
        "item_regions",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("region", sa.String(length=16), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["raw_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "region"),
    )

    op.create_table(
        "item_snapshots",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        _ts("ts", nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        _jsonb("metrics"),
        sa.ForeignKeyConstraint(["item_id"], ["raw_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "ts"),
    )

    op.create_table(
        "topic_items",
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        _now("assigned_at"),
        sa.ForeignKeyConstraint(["item_id"], ["raw_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("topic_id", "item_id"),
    )
    op.create_index("uq_topic_items_item", "topic_items", ["item_id"], unique=True)

    # 默认时间索引与复合主键重复，关闭以保持模型与库一致（alembic check）
    for table, (column, interval) in HYPERTABLES.items():
        op.execute(
            f"SELECT create_hypertable('{table}', '{column}', "
            f"chunk_time_interval => INTERVAL '{interval}', create_default_indexes => false)"
        )


def downgrade() -> None:
    for table in (
        "topic_items",
        "item_snapshots",
        "item_regions",
        "item_embeddings",
        "feedback",
        "evidence",
        "validations",
        "source_runs",
        "raw_items",
        "opportunities",
        "topics",
        "topic_hourly_stats",
        "sources",
        "methodology",
        "llm_usage",
        "keyword_hourly_stats",
        "alerts",
    ):
        op.drop_table(table)
