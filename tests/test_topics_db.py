from datetime import timedelta

import pytest
from sqlalchemy import select, text

from groundswell.collectors.base import Item
from groundswell.collectors.runner import store_items, sync_sources
from groundswell.configs import SourceConfig, SourcesFile, load_calendars, load_regions
from groundswell.db.models import Topic, TopicItem
from groundswell.db.session import session_scope
from groundswell.processing.annotate import annotate_pending
from groundswell.processing.classify import classify_topics
from groundswell.processing.cluster import assign_pending, merge_similar_topics, merge_topics
from groundswell.processing.embed import HashingEmbedding, embed_pending
from groundswell.processing.naming import name_topics
from groundswell.processing.timeutil import now_utc
from groundswell.processing.trend import aggregate_hours, score_hours

pytestmark = pytest.mark.db

A = SourceConfig(
    id="src_a", kind="rss", name="a", region="GLOBAL", language="en", category_hint="tech"
)
B = SourceConfig(id="src_b", kind="rss", name="b", region="NA", country="US", language="en")


def _seed() -> None:
    regions = load_regions()
    now = now_utc() - timedelta(minutes=30)
    with session_scope() as s:
        sync_sources(s, SourcesFile(sources=[A, B]))
    with session_scope() as s:
        store_items(
            s,
            A,
            [
                Item("a1", "DeepSeek releases open model R3 for coding agents"),
                Item("a2", "Weather report for sailors calm seas tonight"),
            ],
            now,
            regions,
        )
    with session_scope() as s:
        store_items(
            s,
            B,
            [
                Item("b1", "DeepSeek releases open model R3 coding agents benchmark"),
                Item("b2", "DeepSeek releases open model R3 for coding agents"),  # 与 a1 重复
            ],
            now + timedelta(minutes=1),
            regions,
        )


def _process() -> HashingEmbedding:
    provider = HashingEmbedding()
    regions = load_regions()
    with session_scope() as s:
        annotate_pending(s, regions)
    with session_scope() as s:
        assert embed_pending(s, provider) == 3  # 重复条目不做 embedding
    with session_scope() as s:
        stats = assign_pending(s, provider)
        assert stats == {"assigned": 3, "new_topics": 2, "dups_attached": 1, "refreshed": 2}
    return provider


def test_assign_refresh_and_topic_stats(db):
    _seed()
    _process()
    with session_scope() as s:
        topics = {t.name: t for t in s.scalars(select(Topic))}
        deep = next(t for n, t in topics.items() if n.startswith("DeepSeek"))
        assert deep.item_count == 3 and deep.source_count == 2
        assert "deepseek" in deep.keywords
        assert "NA" in deep.active_regions
        assert s.scalar(select(TopicItem.topic_id).where(TopicItem.item_id == 1)) == deep.id

        end = now_utc()
        aggregate_hours(s, "topic", end - timedelta(hours=2), end)
        score_hours(
            s,
            "topic",
            end - timedelta(hours=2),
            end,
            window=48,
            min_count=3,
            calendars=load_calendars(),
            regions=load_regions(),
        )
        rows = dict(
            s.execute(
                text("SELECT region, mentions FROM topic_hourly_stats WHERE topic_id = :t"),
                {"t": deep.id},
            ).all()
        )
        assert rows["GLOBAL"] == 3 and rows["NA"] >= 2

    with session_scope() as s:
        assert classify_topics(s) == 2
        assert name_topics(s) == 2
    with session_scope() as s:
        deep = s.get(Topic, deep.id)
        assert deep.category == "tech" and deep.named_at is not None
        assert "DeepSeek" in deep.name


def test_merge_topics_moves_items_and_stats(db):
    _seed()
    provider = _process()
    with session_scope() as s:
        ids = sorted(s.scalars(select(Topic.id)))
        end = now_utc()
        aggregate_hours(s, "topic", end - timedelta(hours=2), end)
    with session_scope() as s:
        merge_topics(s, ids[0], [ids[1]])
    with session_scope() as s:
        keep, gone = s.get(Topic, ids[0]), s.get(Topic, ids[1])
        assert gone.status == "merged" and gone.merged_into == keep.id
        assert keep.item_count == 4
        n = s.execute(
            text("SELECT count(*) FROM topic_hourly_stats WHERE topic_id = :t"), {"t": ids[1]}
        ).scalar_one()
        assert n == 0
    # 低阈值下夜间合并不会再合并已合并的话题
    provider.merge_threshold = 0.01
    with session_scope() as s:
        assert merge_similar_topics(s, provider=provider) == 0
