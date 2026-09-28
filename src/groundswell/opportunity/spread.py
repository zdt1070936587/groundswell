"""跨区扩散路径与区域空白（信息差）检测。

扩散路径：话题在各区域首次突破阈值（z ≥ GS_TREND_Z_THRESHOLD）的时间排序；未突破但有一定讨论量的区域
按首次出现时间排在后面。路径写入 topics.spread_path，路径首个区域即起源区域。

区域空白：话题已在 ≥ GAP_MIN_BREAKOUT 个区域起飞，而某个业务区域“几乎没人讨论”（absent）或
“有讨论但没起飞”（lagging）。空白交给深度分析判断是“还没人做”还是“结构性做不了”。
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import llm
from groundswell.config import get_settings
from groundswell.configs import GLOBAL, load_methodology, load_regions
from groundswell.db.session import session_scope
from groundswell.opportunity import funnel
from groundswell.opportunity.methodology import sync_methodology
from groundswell.processing.timeutil import now_utc
from groundswell.taxonomy import BUSINESS_CATEGORIES

GAP_MIN_BREAKOUT = 2
GAP_LIMIT = 20
ABSENT_MENTIONS = 1.0


@dataclass(frozen=True)
class RegionStat:
    region: str
    first_seen: datetime
    first_breakout: datetime | None
    peak_z: float | None
    mentions: float


def build_path(stats: list[RegionStat], min_mentions: float) -> list[dict[str, Any]]:
    """起飞的区域按突破时间排序在前，只有讨论量的区域按首次出现时间排在后。"""
    rows = [s for s in stats if s.region != GLOBAL]
    rows = [s for s in rows if s.first_breakout is not None or s.mentions >= min_mentions]
    rows.sort(key=lambda s: (s.first_breakout is None, s.first_breakout or s.first_seen, s.region))
    return [
        {
            "region": s.region,
            "first_seen": s.first_seen.isoformat(),
            "first_breakout": s.first_breakout.isoformat() if s.first_breakout else None,
            "peak_z": None if s.peak_z is None else round(s.peak_z, 2),
            "mentions": round(s.mentions, 1),
        }
        for s in rows
    ]


def find_gaps(
    stats: list[RegionStat],
    target_regions: list[str],
    min_breakout: int = GAP_MIN_BREAKOUT,
) -> list[tuple[str, str]]:
    """返回 [(区域, absent|lagging)]。"""
    by_region = {s.region: s for s in stats if s.region != GLOBAL}
    broke = {r for r, s in by_region.items() if s.first_breakout is not None}
    if len(broke) < min_breakout:
        return []
    gaps = []
    for region in target_regions:
        if region == GLOBAL or region in broke:
            continue
        s = by_region.get(region)
        gaps.append((region, "absent" if s is None or s.mentions < ABSENT_MENTIONS else "lagging"))
    return gaps


def load_region_stats(
    session: Session, since: datetime, z_threshold: float, min_count: float
) -> tuple[dict[int, list[RegionStat]], dict[int, str | None]]:
    rows = session.execute(
        text(
            """
            SELECT s.topic_id, s.region, min(s.hour) AS first_seen,
                   min(s.hour) FILTER (WHERE s.z >= :thr AND s.mentions >= :minc) AS first_breakout,
                   max(s.z) AS peak_z, sum(s.mentions) AS mentions, t.category
            FROM topic_hourly_stats s JOIN topics t ON t.id = s.topic_id AND t.status = 'active'
            WHERE s.hour >= :since AND s.region <> 'GLOBAL' AND s.mentions > 0
            GROUP BY s.topic_id, s.region, t.category
            """
        ),
        {"since": since, "thr": z_threshold, "minc": min_count},
    ).all()
    stats: dict[int, list[RegionStat]] = defaultdict(list)
    categories: dict[int, str | None] = {}
    for tid, region, first_seen, breakout, peak_z, mentions, category in rows:
        stats[tid].append(RegionStat(region, first_seen, breakout, peak_z, float(mentions)))
        categories[tid] = category
    return stats, categories


def update_paths(session: Session, stats: dict[int, list[RegionStat]], min_mentions: float) -> int:
    updates = []
    for tid, rows in stats.items():
        path = build_path(rows, min_mentions)
        updates.append(
            {
                "t": tid,
                "p": json.dumps(path, ensure_ascii=False) if path else None,
                "o": path[0]["region"] if path else None,
            }
        )
    if updates:
        session.execute(
            text(
                """
                UPDATE topics SET spread_path = CAST(:p AS jsonb),
                                  origin_region = coalesce(:o, origin_region)
                WHERE id = :t
                """
            ),
            updates,
        )
    return len(updates)


def _recently_analyzed(session: Session, topic_id: int, region: str) -> bool:
    return (
        session.execute(
            text(
                """
                SELECT 1 FROM opportunities WHERE topic_id = :t AND region = :r
                  AND created_at >= now() - make_interval(days => :d) LIMIT 1
                """
            ),
            {"t": topic_id, "r": region, "d": funnel.REANALYZE_DAYS},
        ).first()
        is not None
    )


def analyze_gap_list(gaps: list[tuple[int, str, str]]) -> dict[str, int]:
    """对 [(topic_id, 区域, 原因)] 跑深度分析，共用漏斗的每日 LLM 配额与预算熔断。"""
    settings = get_settings()
    rfile = load_regions()
    methodology = load_methodology()
    with session_scope() as s:
        version = sync_methodology(s, methodology)
        remaining = max(0, settings.llm_deep_daily_cap - funnel._deep_used_today(s))
    use_llm = llm.available("strong")
    counts: dict[str, int] = defaultdict(int)
    for topic_id, region, reason in gaps:
        with session_scope() as s:
            if _recently_analyzed(s, topic_id, region):
                counts["skipped"] += 1
                continue
            c = funnel.candidate_for(s, topic_id, region)
        if c is None:
            continue
        kwargs = {
            "regions": rfile,
            "principles": methodology.principles,
            "methodology_version": version,
            "is_gap": True,
            "gap_reason": reason,
        }
        try:
            _, status = funnel.analyze(c, use_llm=use_llm and remaining > 0, **kwargs)
        except llm.BudgetExceeded as exc:
            logger.warning(f"{exc}，剩余区域空白改用启发式")
            use_llm = False
            _, status = funnel.analyze(c, use_llm=False, **kwargs)
        if status == "llm":
            remaining -= 1
        counts[status] += 1
    return dict(counts)


def run_spread(days: int = 14, analyze_gaps: bool = False) -> dict[str, Any]:
    settings = get_settings()
    rfile = load_regions()
    since = now_utc() - timedelta(days=days)
    targets = [
        r for r in (settings.report_regions or rfile.codes) if r in rfile.regions and r != GLOBAL
    ]
    with session_scope() as s:
        stats, categories = load_region_stats(
            s, since, settings.trend_z_threshold, settings.trend_min_count
        )
        updated = update_paths(s, stats, settings.trend_min_count)
    gaps: list[tuple[int, str, str, int, float]] = []
    for tid, rows in stats.items():
        if categories.get(tid) not in BUSINESS_CATEGORIES:
            continue
        found = find_gaps(rows, targets)
        if not found:
            continue
        n_broke = sum(r.first_breakout is not None for r in rows)
        best_z = max((r.peak_z or 0.0) for r in rows)
        gaps.extend((tid, region, reason, n_broke, best_z) for region, reason in found)
    gaps.sort(key=lambda g: (-g[3], -g[4], g[0], g[1]))
    out: dict[str, Any] = {
        "topics": updated,
        "multi_region": sum(
            sum(r.first_breakout is not None for r in rows) >= 2 for rows in stats.values()
        ),
        "gaps": len(gaps),
    }
    if analyze_gaps and gaps:
        out["analyzed"] = analyze_gap_list([(t, r, why) for t, r, why, _, _ in gaps[:GAP_LIMIT]])
    logger.info(f"扩散路径：{out}")
    return out
