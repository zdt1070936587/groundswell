"""标注阶段：语言 → 关键词 → simhash 去重 → 内容级区域归属。以 processed_at IS NULL 为水位。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from groundswell.configs import GLOBAL, RegionsFile
from groundswell.db.models import ItemRegion, RawItem
from groundswell.processing.dedupe import (
    Fingerprint,
    features,
    find_duplicates,
    fingerprint,
    normalize_title,
    to_signed,
)
from groundswell.processing.keywords import extract_keywords
from groundswell.processing.lang import detect_language
from groundswell.processing.region import attribute
from groundswell.processing.timeutil import now_utc

DEDUPE_WINDOW = timedelta(hours=48)


def annotate_pending(session: Session, regions: RegionsFile, batch: int = 2000) -> int:
    items = list(
        session.scalars(
            select(RawItem).where(RawItem.processed_at.is_(None)).order_by(RawItem.id).limit(batch)
        )
    )
    if not items:
        return 0
    now = now_utc()
    new_fps: list[Fingerprint] = []
    region_rows: list[dict] = []

    for it in items:
        body = f"{it.title} {it.text or ''}"
        lang = it.language or detect_language(it.title if len(it.title) > 8 else body)
        it.language = lang
        it.keywords = extract_keywords(it.title, it.text, lang)
        fp = fingerprint(it.id, it.title, it.text)
        it.simhash = to_signed(fp.simhash)
        new_fps.append(fp)

        attr = attribute(
            ingest_region=it.region,
            ingest_country=it.country,
            ingest_method=it.region_method,
            ingest_confidence=it.region_confidence,
            text=body,
            language=lang,
            regions=regions,
        )
        it.region = attr.region
        it.country = attr.country or it.country
        it.region_method = attr.method
        it.region_confidence = attr.confidence
        for region, weight in attr.weights.items():
            if region != GLOBAL:
                region_rows.append(
                    {"item_id": it.id, "region": region, "weight": weight, "method": attr.method}
                )
        it.processed_at = now

    # 与最近 48 小时已处理、非重复的条目比对
    oldest = min(it.fetched_at for it in items) - DEDUPE_WINDOW
    existing = [
        Fingerprint(i, sh, t, n)
        for i, sh, t, n in (
            (row.id, row.simhash, *_norm(row.title, row.text))
            for row in session.execute(
                select(RawItem.id, RawItem.simhash, RawItem.title, RawItem.text).where(
                    RawItem.fetched_at >= oldest,
                    RawItem.processed_at.is_not(None),
                    RawItem.dup_of.is_(None),
                    RawItem.id.not_in([it.id for it in items]),
                )
            )
        )
        if sh is not None
    ]
    dups = find_duplicates(existing, new_fps)
    for it in items:
        if it.id in dups:
            it.dup_of = dups[it.id]

    if region_rows:
        stmt = insert(ItemRegion).values(region_rows)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["item_id", "region"],
                set_={"weight": stmt.excluded.weight, "method": stmt.excluded.method},
            )
        )
    session.flush()
    return len(items)


def _norm(title: str, text: str | None) -> tuple[str, int]:
    body = f"{title} {text[:300]}" if text else title
    return normalize_title(title), len(features(body))
