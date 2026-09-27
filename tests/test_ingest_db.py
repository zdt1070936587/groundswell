from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from groundswell.collectors.base import Item
from groundswell.collectors.runner import store_items, sync_sources
from groundswell.configs import SourceConfig, SourcesFile, load_regions
from groundswell.db.models import ItemSnapshot, RawItem, Source
from groundswell.db.session import session_scope

pytestmark = pytest.mark.db

CFG = SourceConfig(id="test_src", kind="rss", name="test", region="GC", country="CN", language="zh")


def test_upsert_is_idempotent_and_snapshots_accumulate(db):
    regions = load_regions()
    t0 = datetime(2026, 9, 27, 10, tzinfo=UTC)
    with session_scope() as s:
        sync_sources(s, SourcesFile(sources=[CFG]))
    with session_scope() as s:
        total, new = store_items(
            s, CFG, [Item("a", "标题A", metrics={"heat": 1}, rank=1)], t0, regions
        )
        assert (total, new) == (1, 1)
    with session_scope() as s:
        total, new = store_items(
            s,
            CFG,
            [Item("a", "改过的标题", metrics={"heat": 5}, rank=3), Item("b", "标题B")],
            t0 + timedelta(hours=1),
            regions,
        )
        assert (total, new) == (2, 1)
    with session_scope() as s:
        row = s.scalar(select(RawItem).where(RawItem.external_id == "a"))
        assert row.title == "标题A"  # 内容字段不可变
        assert row.metrics == {"heat": 5} and row.rank == 3  # 观测字段刷新
        assert row.fetched_at == t0 and row.last_seen_at == t0 + timedelta(hours=1)
        assert row.region == "GC" and row.country == "CN"
        assert s.scalar(select(func.count()).select_from(ItemSnapshot)) == 3
        assert s.get(Source, "test_src").enabled
