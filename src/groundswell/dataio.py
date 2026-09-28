"""历史数据导入导出（JSONL，每行一个条目）。

导入：回测需要覆盖黄金集时间段的数据。导入条目的 fetched_at / 快照时间取 published_at，
因此历史数据落在它自己的时间点上，不会冲高“当前小时”的统计；导入后照常由 annotate 标注。

字段：title（必填）、published_at（必填，ISO 8601 或秒/毫秒时间戳；缺省时取 fetched_at，兼容 gs data export）、
可选 external_id / url / text / country / language / metrics / rank / channel / author。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from groundswell.collectors.base import MAX_TEXT, Item, from_epoch, parse_iso
from groundswell.collectors.runner import build_rows
from groundswell.configs import SourceConfig, load_regions
from groundswell.db.models import ItemSnapshot, RawItem, Source
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import now_utc

BATCH = 500

_EXPORT_FIELDS = (
    "source_id",
    "external_id",
    "channel",
    "url",
    "title",
    "text",
    "published_at",
    "fetched_at",
    "metrics",
    "rank",
    "language",
    "country",
    "region",
)


def parse_line(obj: dict[str, Any]) -> Item | None:
    title = str(obj.get("title") or "").strip()
    raw_ts = obj.get("published_at") or obj.get("fetched_at")
    published = (
        from_epoch(raw_ts) if isinstance(raw_ts, int | float) else parse_iso(str(raw_ts or ""))
    )
    if not title or published is None:
        return None
    ext = (
        obj.get("external_id")
        or hashlib.sha1(
            f"{title}|{published.isoformat()}|{obj.get('url') or ''}".encode()
        ).hexdigest()
    )
    return Item(
        external_id=str(ext),
        title=title,
        url=obj.get("url"),
        text=(obj.get("text") or None) and str(obj["text"])[:MAX_TEXT],
        author=obj.get("author"),
        published_at=published,
        metrics=obj.get("metrics") or {},
        channel=obj.get("channel"),
        country=obj.get("country"),
        language=obj.get("language"),
        rank=obj.get("rank"),
    )


def _read(path: Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(f"{path}:{n} 不是合法 JSON，跳过：{exc}")


def ensure_source(session: Session, cfg: SourceConfig) -> None:
    if session.get(Source, cfg.id) is not None:
        return
    session.add(
        Source(
            id=cfg.id,
            kind=cfg.kind,
            name=cfg.name,
            default_region=cfg.region,
            default_country=cfg.country,
            default_language=cfg.language,
            config=cfg.model_dump(mode="json"),
            enabled=False,
            consecutive_failures=0,
            last_item_count=0,
        )
    )
    session.flush()


def store_historical(session: Session, cfg: SourceConfig, items: list[Item]) -> int:
    regions = load_regions()
    rows = build_rows(cfg, items, now_utc(), regions)
    if not rows:
        return 0
    for row in rows:
        row["fetched_at"] = row["last_seen_at"] = row["published_at"]
    inserted = 0
    for start in range(0, len(rows), BATCH):
        chunk = rows[start : start + BATCH]
        stmt = (
            insert(RawItem)
            .values(chunk)
            .on_conflict_do_nothing(constraint="uq_raw_items_source_external")
            .returning(RawItem.id, RawItem.external_id)
        )
        by_ext = {r["external_id"]: r for r in chunk}
        snapshots = [
            {
                "item_id": item_id,
                "ts": by_ext[ext]["published_at"],
                "rank": by_ext[ext]["rank"],
                "metrics": by_ext[ext]["metrics"],
            }
            for item_id, ext in session.execute(stmt)
        ]
        if snapshots:
            session.execute(insert(ItemSnapshot).values(snapshots).on_conflict_do_nothing())
        inserted += len(snapshots)
    return inserted


def import_jsonl(path: Path, source_id: str = "import", region: str = "GLOBAL") -> int:
    regions = load_regions()
    if region not in regions.regions:
        raise ValueError(f"未知区域 {region}，可选 {regions.codes}")
    cfg = SourceConfig(
        id=source_id, kind="import", name=f"导入：{source_id}", region=region, enabled=False
    )
    total = skipped = 0
    batch: list[Item] = []
    with session_scope() as s:
        ensure_source(s, cfg)
    for obj in _read(path):
        item = parse_line(obj)
        if item is None:
            skipped += 1
            continue
        batch.append(item)
        if len(batch) >= BATCH:
            with session_scope() as s:
                total += store_historical(s, cfg, batch)
            batch = []
    if batch:
        with session_scope() as s:
            total += store_historical(s, cfg, batch)
    if skipped:
        logger.warning(f"跳过 {skipped} 行（缺 title 或 published_at）")
    logger.info(f"导入 {path} → {source_id}/{region}：新增 {total} 条")
    return total


def export_jsonl(path: Path, days: int = 30) -> int:
    since = now_utc() - timedelta(days=days)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [getattr(RawItem, f) for f in _EXPORT_FIELDS]
    n = 0
    with session_scope() as s, path.open("w", encoding="utf-8") as fh:
        result = s.execute(
            select(*cols).where(RawItem.fetched_at >= since).order_by(RawItem.id),
            execution_options={"yield_per": 2000},
        )
        for row in result:
            obj = dict(zip(_EXPORT_FIELDS, row, strict=True))
            for key in ("published_at", "fetched_at"):
                if obj[key] is not None:
                    obj[key] = obj[key].isoformat()
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            n += 1
    return n
