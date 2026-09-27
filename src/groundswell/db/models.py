"""数据模型。

原则：`raw_items` 的内容字段（标题、正文、链接、发布时间）写入后不再修改；
标注字段（语言、区域、关键词、simhash、dup_of、processed_at）以及其他所有表都是派生数据，
可以从原始数据重建。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 1024


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        list[str]: ARRAY(String),
        datetime: DateTime(timezone=True),
    }


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


# ---------------------------------------------------------------- 采集层


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    default_region: Mapped[str] = mapped_column(String(16))
    default_country: Mapped[str | None] = mapped_column(String(8))
    default_language: Mapped[str | None] = mapped_column(String(16))
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_item_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class SourceRun(Base):
    __tablename__ = "source_runs"
    __table_args__ = (Index("ix_source_runs_source_started", "source_id", "started_at"),)

    id: Mapped[int] = _pk()
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    started_at: Mapped[datetime]
    duration_ms: Mapped[int] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    items: Mapped[int] = mapped_column(Integer, default=0)
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class RawItem(Base):
    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_items_source_external"),
        Index("ix_raw_items_fetched_at", "fetched_at"),
        Index("ix_raw_items_region", "region"),
        Index("ix_raw_items_unprocessed", "id", postgresql_where=sql_text("processed_at IS NULL")),
    )

    id: Mapped[int] = _pk()
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    external_id: Mapped[str] = mapped_column(String(512))
    channel: Mapped[str | None] = mapped_column(String(128))
    url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str | None] = mapped_column(Text)
    author_hash: Mapped[str | None] = mapped_column(String(32))
    published_at: Mapped[datetime | None]
    fetched_at: Mapped[datetime]
    # 观测字段：最近一次抓取到的互动数据与榜单位置，历史见 item_snapshots
    last_seen_at: Mapped[datetime]
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)
    rank: Mapped[int | None] = mapped_column(Integer)
    # 标注字段
    language: Mapped[str | None] = mapped_column(String(16))
    country: Mapped[str | None] = mapped_column(String(8))
    region: Mapped[str | None] = mapped_column(String(16))
    region_confidence: Mapped[float | None] = mapped_column(Float)
    region_method: Mapped[str | None] = mapped_column(String(16))
    keywords: Mapped[list[str] | None]
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    dup_of: Mapped[int | None] = mapped_column(ForeignKey("raw_items.id"))
    processed_at: Mapped[datetime | None]


class ItemSnapshot(Base):
    """Timescale hypertable（按 ts 分区）。"""

    __tablename__ = "item_snapshots"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_items.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    rank: Mapped[int | None] = mapped_column(Integer)
    metrics: Mapped[dict[str, Any]] = mapped_column(default=dict)


class ItemRegion(Base):
    __tablename__ = "item_regions"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_items.id", ondelete="CASCADE"), primary_key=True
    )
    region: Mapped[str] = mapped_column(String(16), primary_key=True)
    weight: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(16))


class ItemEmbedding(Base):
    __tablename__ = "item_embeddings"

    item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_items.id", ondelete="CASCADE"), primary_key=True
    )
    model: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------- 话题层


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        Index("ix_topics_status_last_seen", "status", "last_seen"),
        Index(
            "ix_topics_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(16))
    category_confidence: Mapped[float | None] = mapped_column(Float)
    keywords: Mapped[list[str]] = mapped_column(default=list)
    embedding: Mapped[Any | None] = mapped_column(Vector(EMBED_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    origin_region: Mapped[str | None] = mapped_column(String(16))
    active_regions: Mapped[list[str]] = mapped_column(default=list)
    spread_path: Mapped[list[Any] | None]
    slow_variables: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="active")
    merged_into: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]
    named_at: Mapped[datetime | None]
    classified_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TopicItem(Base):
    __tablename__ = "topic_items"
    __table_args__ = (Index("uq_topic_items_item", "item_id", unique=True),)

    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_items.id", ondelete="CASCADE"), primary_key=True
    )
    similarity: Mapped[float] = mapped_column(Float)
    assigned_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TopicHourlyStat(Base):
    """Timescale hypertable（按 hour 分区）。每小时另有 region='GLOBAL' 的汇总行。"""

    __tablename__ = "topic_hourly_stats"

    topic_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    region: Mapped[str] = mapped_column(String(16), primary_key=True)
    hour: Mapped[datetime] = mapped_column(primary_key=True)
    mentions: Mapped[float] = mapped_column(Float, default=0)
    new_items: Mapped[float] = mapped_column(Float, default=0)
    engagement: Mapped[float] = mapped_column(Float, default=0)
    sources_count: Mapped[int] = mapped_column(Integer, default=0)
    z: Mapped[float | None] = mapped_column(Float)
    growth: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)


class KeywordHourlyStat(Base):
    """聚类之前的关键词级热度（Timescale hypertable）。"""

    __tablename__ = "keyword_hourly_stats"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    region: Mapped[str] = mapped_column(String(16), primary_key=True)
    hour: Mapped[datetime] = mapped_column(primary_key=True)
    mentions: Mapped[float] = mapped_column(Float, default=0)
    new_items: Mapped[float] = mapped_column(Float, default=0)
    sources_count: Mapped[int] = mapped_column(Integer, default=0)
    z: Mapped[float | None] = mapped_column(Float)
    growth: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)


# ---------------------------------------------------------------- 商机层


class MethodologyPrinciple(Base):
    __tablename__ = "methodology"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    person: Mapped[str] = mapped_column(String(64))
    principle: Mapped[str] = mapped_column(Text)
    question: Mapped[str] = mapped_column(Text)
    tag: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    region_specific: Mapped[bool] = mapped_column(Boolean, default=False)
    signals: Mapped[list[str]] = mapped_column(default=list)
    version: Mapped[str] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("topic_id", "region", "input_hash", name="uq_opportunities_input"),
        Index("ix_opportunities_created", "created_at"),
    )

    id: Mapped[int] = _pk()
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    region: Mapped[str] = mapped_column(String(16))
    input_hash: Mapped[str] = mapped_column(String(40))
    prompt_version: Mapped[str] = mapped_column(String(32))
    methodology_version: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)
    scores: Mapped[dict[str, Any]] = mapped_column(default=dict)
    total_score: Mapped[float] = mapped_column(Float, default=0)
    rank_score: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list[str]] = mapped_column(default=list)
    why_now: Mapped[str | None] = mapped_column(Text)
    distribution_path: Mapped[str | None] = mapped_column(Text)
    product_shapes: Mapped[list[Any]] = mapped_column(default=list)
    counter_arguments: Mapped[list[Any]] = mapped_column(default=list)
    known_players: Mapped[list[Any]] = mapped_column(default=list)
    durability: Mapped[str] = mapped_column(String(16), default="unknown")
    is_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    gap_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_opportunity", "opportunity_id"),)

    id: Mapped[int] = _pk()
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    snippet: Mapped[str] = mapped_column(Text)
    value: Mapped[dict[str, Any] | None]


class Validation(Base):
    __tablename__ = "validations"
    __table_args__ = (Index("ix_validations_lookup", "topic_id", "region", "kind", "ts"),)

    id: Mapped[int] = _pk()
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    region: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(32))
    query: Mapped[str] = mapped_column(Text)
    signal: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())


class Feedback(Base):
    __tablename__ = "feedback"
    __table_args__ = (Index("ix_feedback_opportunity", "opportunity_id"),)

    id: Mapped[int] = _pk()
    opportunity_id: Mapped[int] = mapped_column(ForeignKey("opportunities.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(8))
    note: Mapped[str | None] = mapped_column(Text)
    user_name: Mapped[str] = mapped_column(String(64), default="me")
    ts: Mapped[datetime] = mapped_column(server_default=func.now())


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_ts", "ts"),)

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    model: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)


class Alert(Base):
    """告警与推送去重记录。"""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_kind_key_ts", "kind", "key", "ts"),)

    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------- 点子库与订阅（Phase 5）


class Idea(Base):
    __tablename__ = "ideas"
    __table_args__ = (Index("ix_ideas_status", "status"),)

    id: Mapped[int] = _pk()
    opportunity_id: Mapped[int | None] = mapped_column(
        ForeignKey("opportunities.id", ondelete="SET NULL"), unique=True
    )
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id", ondelete="SET NULL"))
    region: Mapped[str] = mapped_column(String(16), default="GLOBAL")
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    owner: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class IdeaEvent(Base):
    __tablename__ = "idea_events"
    __table_args__ = (Index("ix_idea_events_idea", "idea_id"),)

    id: Mapped[int] = _pk()
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text)
    user_name: Mapped[str] = mapped_column(String(64), default="me")
    ts: Mapped[datetime] = mapped_column(server_default=func.now())


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128))
    keywords: Mapped[list[str]] = mapped_column(default=list)
    regions: Mapped[list[str]] = mapped_column(default=list)
    categories: Mapped[list[str]] = mapped_column(default=list)
    min_score: Mapped[float] = mapped_column(Float, default=2.0)
    channel: Mapped[str] = mapped_column(Text, default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SubscriptionHit(Base):
    __tablename__ = "subscription_hits"
    __table_args__ = (
        UniqueConstraint("subscription_id", "topic_id", "region", name="uq_subscription_hits"),
    )

    id: Mapped[int] = _pk()
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id", ondelete="CASCADE"))
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    region: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())


# ---------------------------------------------------------------- 协作（Phase 6）


class IdeaComment(Base):
    __tablename__ = "idea_comments"
    __table_args__ = (Index("ix_idea_comments_idea", "idea_id"),)

    id: Mapped[int] = _pk()
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id", ondelete="CASCADE"))
    user_name: Mapped[str] = mapped_column(String(64), default="me")
    body: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
