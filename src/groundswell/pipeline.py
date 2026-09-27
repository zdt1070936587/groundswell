"""流水线编排。调度器、Redis worker、CLI 都调用这里的函数；同名阶段由 advisory lock 互斥。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

from loguru import logger

from groundswell.config import get_settings
from groundswell.configs import load_calendars, load_regions
from groundswell.db.session import advisory_lock, session_scope
from groundswell.processing.timeutil import now_utc

STAGES = ("annotate", "keywords", "embed", "cluster", "topics")


def locked(name: str, fn: Callable[[], Any]) -> Any:
    with advisory_lock(name) as acquired:
        if not acquired:
            logger.info(f"阶段 {name} 正在其他进程中运行，跳过")
            return "skipped:locked"
        return fn()


def _annotate() -> int:
    from groundswell.processing.annotate import annotate_pending

    regions, total = load_regions(), 0
    for _ in range(50):
        with session_scope() as s:
            n = annotate_pending(s, regions)
        total += n
        if n == 0:
            break
    return total


def _stats(kind: str, hours: int) -> dict[str, int]:
    from groundswell.processing.trend import aggregate_hours, score_hours

    settings = get_settings()
    end = now_utc()
    start = end - timedelta(hours=hours)
    with session_scope() as s:
        rows = aggregate_hours(s, kind, start, end)  # type: ignore[arg-type]
        scored = score_hours(
            s,
            kind,  # type: ignore[arg-type]
            start,
            end,
            window=settings.trend_window_hours,
            min_count=settings.trend_min_count,
            calendars=load_calendars(),
            regions=load_regions(),
        )
    return {"rows": rows, "scored": scored}


def _embed() -> int:
    from groundswell.processing.embed import embed_pending, get_provider

    provider, total = get_provider(), 0
    for _ in range(20):
        with session_scope() as s:
            n = embed_pending(s, provider)
        total += n
        if n == 0:
            break
    return total


def _cluster() -> dict[str, int]:
    from groundswell.processing.cluster import assign_pending
    from groundswell.processing.embed import get_provider

    with session_scope() as s:
        return assign_pending(s, get_provider())


def _topics(hours: int) -> dict[str, Any]:
    from groundswell.processing.classify import classify_topics
    from groundswell.processing.slow_vars import tag_slow_variables

    out: dict[str, Any] = {"stats": _stats("topic", hours)}
    with session_scope() as s:
        out["classified"] = classify_topics(s)
        out["slow_variables"] = tag_slow_variables(s)
    return out


def process(stage: str = "all", hours: int = 3) -> dict[str, Any]:
    if stage != "all" and stage not in STAGES:
        raise ValueError(f"未知阶段 {stage}，可选 {STAGES}")

    def run(name: str) -> bool:
        return stage in ("all", name)

    stats: dict[str, Any] = {}
    if run("annotate"):
        stats["annotate"] = locked("annotate", _annotate)
    if run("keywords"):
        stats["keywords"] = locked("keyword_stats", lambda: _stats("keyword", hours))
    if run("embed"):
        stats["embed"] = locked("embed", _embed)
    if run("cluster"):
        stats["cluster"] = locked("cluster", _cluster)
    if run("topics"):
        stats["topics"] = locked("topic_stats", lambda: _topics(hours))
    return stats


# ---------------------------------------------------------------- 定时流水线


def run_collect_due() -> dict[str, Any]:
    from groundswell.collectors.runner import collect

    results = collect(only_due=True)
    return {
        "sources": len(results),
        "ok": sum(r.ok for r in results),
        "new": sum(r.new_items for r in results),
    }


def run_hourly(collect_first: bool = True) -> dict[str, Any]:
    from groundswell.ops.health import check_and_alert
    from groundswell.subscriptions import check_subscriptions

    out: dict[str, Any] = {}
    if collect_first:
        out["collect"] = run_collect_due()
    out["process"] = process("all", hours=3)
    out["subscriptions"] = locked("subscriptions", check_subscriptions)
    out["health_alerts"] = check_and_alert()
    return out


def run_daily(push: bool = True) -> dict[str, Any]:
    from groundswell.opportunity.funnel import run_funnel
    from groundswell.opportunity.ranker import apply as rank_apply
    from groundswell.opportunity.spread import run_spread
    from groundswell.opportunity.validators import run_validations
    from groundswell.processing.naming import name_topics
    from groundswell.reporting.daily import build_and_save_daily

    out: dict[str, Any] = {}

    def _name() -> int:
        with session_scope() as s:
            return name_topics(s)

    out["named"] = locked("naming", _name)
    out["spread"] = locked("spread", lambda: run_spread(days=14, analyze_gaps=False))
    out["funnel"] = locked("funnel", run_funnel)
    out["gaps"] = locked("spread_gaps", lambda: run_spread(days=14, analyze_gaps=True))
    out["validations"] = locked("validations", run_validations)
    out["ranked"] = rank_apply(days=7)
    path, _ = build_and_save_daily(push=push)
    out["report"] = str(path)
    return out


def run_nightly() -> dict[str, Any]:
    from groundswell.opportunity.ranker import train
    from groundswell.processing.cluster import merge_similar_topics

    out: dict[str, Any] = {}

    def _merge() -> int:
        with session_scope() as s:
            return merge_similar_topics(s, days=7)

    out["merged"] = locked("cluster", _merge)
    # 合并后重算近 7 天的话题统计
    out["topic_stats"] = locked("topic_stats", lambda: _stats("topic", 24 * 7))
    out["ranker"] = train()
    return out


def run_weekly(push: bool = True) -> str:
    from groundswell.reporting.periodic import build_and_save_periodic

    path, _ = build_and_save_periodic("weekly", push=push)
    return str(path)


def run_monthly(push: bool = True) -> str:
    from groundswell.reporting.periodic import build_and_save_periodic

    path, _ = build_and_save_periodic("monthly", push=push)
    return str(path)
