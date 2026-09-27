from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from groundswell.collectors.base import Item
from groundswell.collectors.runner import store_items, sync_sources
from groundswell.configs import SourceConfig, SourcesFile, load_calendars, load_regions
from groundswell.db.models import ItemRegion, RawItem
from groundswell.db.session import session_scope
from groundswell.processing.annotate import annotate_pending
from groundswell.processing.trend import aggregate_hours, score_hours

pytestmark = pytest.mark.db

GLOBAL_SRC = SourceConfig(id="g_src", kind="rss", name="global", region="GLOBAL", language="en")
GC_SRC = SourceConfig(id="gc_src", kind="rss", name="gc", region="GC", country="CN", language="zh")

T0 = datetime(2026, 9, 1, 0, tzinfo=UTC)


def seed_series(hours: int = 30, burst_from: int = 26) -> None:
    """GLOBAL 源每小时 3 条常规新闻；最后几个小时关键词 'vibecoding' 爆发。"""
    regions = load_regions()
    with session_scope() as s:
        sync_sources(s, SourcesFile(sources=[GLOBAL_SRC, GC_SRC]))
    for h in range(hours):
        ts = T0 + timedelta(hours=h)
        items = [
            Item(f"base-{h}-{i}", f"Weather report number {h} {i} for sailors", text="calm seas")
            for i in range(3)
        ]
        if h >= burst_from:
            n = 4 * (h - burst_from + 1)
            items += [
                Item(f"burst-{h}-{i}", f"Vibecoding tool {i} launched in Indonesia hour {h}")
                for i in range(n)
            ]
        with session_scope() as s:
            store_items(s, GLOBAL_SRC, items, ts, regions)
        with session_scope() as s:
            store_items(s, GC_SRC, [Item(f"gc-{h}", f"淄博烧烤第{h}天依然火爆")], ts, regions)


def test_annotate_keyword_trend_and_report(db):
    seed_series()
    regions = load_regions()
    with session_scope() as s:
        assert annotate_pending(s, regions) > 0
    with session_scope() as s:
        burst = s.scalar(select(RawItem).where(RawItem.external_id == "burst-29-0"))
        assert burst.language == "en"
        assert "vibecoding" in burst.keywords
        # 全球源 + 内容里提到 Indonesia → 内容级归属 SEA
        assert burst.region == "SEA" and burst.region_method == "content"
        weights = dict(
            s.execute(
                select(ItemRegion.region, ItemRegion.weight).where(ItemRegion.item_id == burst.id)
            ).all()
        )
        assert weights["SEA"] >= 0.5
        gc = s.scalar(select(RawItem).where(RawItem.external_id == "gc-3"))
        assert gc.language == "zh" and gc.region == "GC" and gc.region_method == "source"

    end = T0 + timedelta(hours=30)
    with session_scope() as s:
        assert aggregate_hours(s, "keyword", T0, end) > 0
        score_hours(
            s,
            "keyword",
            T0,
            end,
            window=8,
            min_count=3,
            calendars=load_calendars(),
            regions=regions,
        )
    with session_scope() as s:
        rows = s.execute(
            text(
                "SELECT region, hour, mentions, z FROM keyword_hourly_stats "
                "WHERE key = 'vibecoding' ORDER BY region, hour"
            )
        ).all()
        by_region = {}
        for region, hour, mentions, z in rows:
            by_region.setdefault(region, []).append((hour, mentions, z))
        assert max(z for _, _, z in by_region["GLOBAL"] if z is not None) >= 2
        assert "SEA" in by_region
        # 冷启动保护：前 4 小时（window // 2）不给 z
        early = s.execute(
            text("SELECT count(*) FROM keyword_hourly_stats WHERE hour < :h AND z IS NOT NULL"),
            {"h": T0 + timedelta(hours=4)},
        ).scalar_one()
        assert early == 0

    from groundswell.reporting.daily import build_daily

    with session_scope() as s:
        md = build_daily(s, day=T0.date() + timedelta(days=1), regions=["GLOBAL", "GC", "SEA"])
    assert "# 观潮日报" in md
    assert "### GC" in md and "淄博烧烤" in md
    assert "vibecoding" in md
    assert "## 数据源健康" in md


def test_duplicates_marked_but_still_counted(db):
    regions = load_regions()
    with session_scope() as s:
        sync_sources(s, SourcesFile(sources=[GLOBAL_SRC, GC_SRC]))
    title = "OpenAI announces a new open weights reasoning model for developers"
    with session_scope() as s:
        store_items(s, GLOBAL_SRC, [Item("x1", title)], T0, regions)
    with session_scope() as s:
        store_items(s, GC_SRC, [Item("x2", title + "!")], T0 + timedelta(minutes=5), regions)
    with session_scope() as s:
        annotate_pending(s, regions)
    with session_scope() as s:
        a = s.scalar(select(RawItem).where(RawItem.external_id == "x1"))
        b = s.scalar(select(RawItem).where(RawItem.external_id == "x2"))
        assert a.dup_of is None and b.dup_of == a.id
        aggregate_hours(s, "keyword", T0, T0 + timedelta(hours=1))
        m = s.execute(
            text("SELECT mentions FROM keyword_hourly_stats WHERE key='openai' AND region='GLOBAL'")
        ).scalar_one()
        assert m == 2  # 转载本身是热度信号
