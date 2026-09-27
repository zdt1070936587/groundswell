"""采集执行：调用采集器 → 幂等 upsert raw_items → 写快照 → 更新源健康。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from groundswell.collectors.base import Item
from groundswell.collectors.registry import build_collector, make_client
from groundswell.config import Settings, get_settings
from groundswell.configs import (
    GLOBAL,
    RegionsFile,
    SourceConfig,
    SourcesFile,
    load_regions,
    load_sources,
)
from groundswell.db.models import ItemSnapshot, RawItem, Source, SourceRun
from groundswell.db.session import session_scope


@dataclass
class RunResult:
    source_id: str
    ok: bool
    items: int = 0
    new_items: int = 0
    duration_ms: int = 0
    error: str | None = None


def sync_sources(session: Session, sources: SourcesFile) -> None:
    """把 sources.yaml 同步到 sources 表（yaml 为唯一事实来源）。"""
    existing = {s.id: s for s in session.scalars(select(Source))}
    for cfg in sources.sources:
        row = existing.get(cfg.id) or Source(id=cfg.id, consecutive_failures=0, last_item_count=0)
        row.kind = cfg.kind
        row.name = cfg.name
        row.default_region = cfg.region
        row.default_country = cfg.country
        row.default_language = cfg.language
        row.config = cfg.model_dump(mode="json")
        row.enabled = cfg.enabled
        session.add(row)
    for sid, row in existing.items():
        if sources.get(sid) is None:
            row.enabled = False


def author_hash(source_id: str, author: str | None) -> str | None:
    if not author:
        return None
    return hashlib.sha256(f"{source_id}:{author}".encode()).hexdigest()[:32]


def resolve_region(
    cfg: SourceConfig, item: Item, regions: RegionsFile
) -> tuple[str, str | None, str, float]:
    """源级 / 子频道级区域归属，返回 (region, country, method, confidence)。"""
    ch = cfg.channel(item.channel)
    country = item.country or (ch.country if ch else None)
    if country:
        region = regions.region_of_country(country)
        if region:
            return region, country.upper(), "channel", 0.95
    if ch and ch.region:
        return ch.region, None, "channel", 0.9
    if cfg.country:
        return regions.region_of_country(cfg.country) or cfg.region, cfg.country, "source", 1.0
    return cfg.region, None, "source", 1.0 if cfg.region != GLOBAL else 0.0


def build_rows(
    cfg: SourceConfig, items: list[Item], fetched_at: datetime, regions: RegionsFile
) -> list[dict]:
    rows: dict[str, dict] = {}
    for it in items:
        ext = it.external_id[:512]
        if ext in rows:
            continue
        region, country, method, conf = resolve_region(cfg, it, regions)
        ch = cfg.channel(it.channel)
        rows[ext] = {
            "source_id": cfg.id,
            "external_id": ext,
            "channel": (it.channel or None) and it.channel[:128],
            "url": it.url,
            "title": it.title[:2000],
            "text": it.text,
            "author_hash": author_hash(cfg.id, it.author),
            "published_at": it.published_at,
            "fetched_at": fetched_at,
            "last_seen_at": fetched_at,
            "metrics": it.metrics,
            "rank": it.rank,
            "language": it.language or (ch.language if ch else None) or cfg.language,
            "country": country,
            "region": region,
            "region_method": method,
            "region_confidence": conf,
        }
    return list(rows.values())


def store_items(
    session: Session,
    cfg: SourceConfig,
    items: list[Item],
    fetched_at: datetime,
    regions: RegionsFile,
) -> tuple[int, int]:
    rows = build_rows(cfg, items, fetched_at, regions)
    if not rows:
        return 0, 0
    new = 0
    snapshots: list[dict] = []
    for start in range(0, len(rows), 500):
        chunk = rows[start : start + 500]
        stmt = insert(RawItem).values(chunk)
        # 内容字段不可变；只刷新观测字段
        stmt = stmt.on_conflict_do_update(
            constraint="uq_raw_items_source_external",
            set_={
                "metrics": stmt.excluded.metrics,
                "rank": stmt.excluded.rank,
                "last_seen_at": stmt.excluded.last_seen_at,
            },
        ).returning(RawItem.id, RawItem.external_id, literal_column("(xmax = 0)").label("inserted"))
        by_ext = {r["external_id"]: r for r in chunk}
        for item_id, ext, inserted in session.execute(stmt):
            new += int(bool(inserted))
            row = by_ext[ext]
            snapshots.append(
                {
                    "item_id": item_id,
                    "ts": fetched_at,
                    "rank": row["rank"],
                    "metrics": row["metrics"],
                }
            )
    session.execute(insert(ItemSnapshot).values(snapshots).on_conflict_do_nothing())
    return len(rows), new


def run_source(
    cfg: SourceConfig,
    settings: Settings | None = None,
    regions: RegionsFile | None = None,
    client: httpx.Client | None = None,
) -> RunResult:
    settings = settings or get_settings()
    regions = regions or load_regions()
    own_client = client is None
    client = client or make_client(settings)
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    result = RunResult(source_id=cfg.id, ok=False)
    try:
        items = build_collector(cfg, settings, client).fetch()
        with session_scope() as session:
            result.items, result.new_items = store_items(session, cfg, items, started, regions)
        result.ok = True
    except Exception as exc:  # noqa: BLE001  单源失败不影响其他源
        result.error = f"{type(exc).__name__}: {exc}"[:2000]
        logger.error(f"采集失败 {cfg.id}: {result.error}")
    finally:
        if own_client:
            client.close()
    result.duration_ms = int((time.perf_counter() - t0) * 1000)

    with session_scope() as session:
        src = session.get(Source, cfg.id)
        if src is None:
            sync_sources(session, SourcesFile(sources=[cfg]))
            session.flush()
            src = session.get(Source, cfg.id)
        src.last_run_at = started
        if result.ok:
            src.last_success_at = started
            src.consecutive_failures = 0
            src.last_error = None
            src.last_item_count = result.items
        else:
            src.consecutive_failures = (src.consecutive_failures or 0) + 1
            src.last_error = result.error
        session.add(
            SourceRun(
                source_id=cfg.id,
                started_at=started,
                duration_ms=result.duration_ms,
                ok=result.ok,
                items=result.items,
                new_items=result.new_items,
                error=result.error,
            )
        )
    logger.info(
        f"采集 {cfg.id}: ok={result.ok} items={result.items} new={result.new_items} {result.duration_ms}ms"
    )
    return result


def due_sources(
    session: Session, sources: SourcesFile, now: datetime | None = None
) -> list[SourceConfig]:
    now = now or datetime.now(UTC)
    last = {s.id: s.last_run_at for s in session.scalars(select(Source))}
    due = []
    for cfg in sources.sources:
        if not cfg.enabled:
            continue
        prev = last.get(cfg.id)
        # 留 2 分钟余量，避免调度抖动导致整点任务被跳过
        if prev is None or now - prev >= timedelta(minutes=cfg.interval_minutes) - timedelta(
            minutes=2
        ):
            due.append(cfg)
    return due


def collect(source_ids: list[str] | None = None, only_due: bool = False) -> list[RunResult]:
    settings = get_settings()
    sources = load_sources()
    regions = load_regions()
    with session_scope() as session:
        sync_sources(session, sources)
        if source_ids:
            unknown = [s for s in source_ids if sources.get(s) is None]
            if unknown:
                raise ValueError(f"未知数据源: {unknown}")
            targets = [sources.get(s) for s in source_ids]
        elif only_due:
            targets = due_sources(session, sources)
        else:
            targets = [s for s in sources.sources if s.enabled]
    results = []
    with make_client(settings) as client:
        for cfg in targets:
            results.append(run_source(cfg, settings, regions, client))
    return results
