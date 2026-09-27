"""报告、API、Dashboard 共用的只读查询。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.processing.trend import _ENGAGEMENT_SQL
from groundswell.taxonomy import BUSINESS_CATEGORIES


def _rows(session: Session, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in session.execute(text(sql), params)]


# ---------------------------------------------------------------- 条目与关键词（聚类之前可用）


def item_heat(
    session: Session, region: str, start: datetime, end: datetime, limit: int = 10
) -> list[dict]:
    """区域内被观测到的条目按“源内分位”排序：跨平台互动量不可比，先在源内归一化。"""
    return _rows(
        session,
        f"""
        WITH obs AS (
            SELECT DISTINCT s.item_id FROM item_snapshots s WHERE s.ts >= :start AND s.ts < :end
        ), items AS (
            SELECT i.id, i.title, i.url, i.source_id, i.region, {_ENGAGEMENT_SQL} AS eng
            FROM obs JOIN raw_items i ON i.id = obs.item_id
            WHERE i.dup_of IS NULL
              AND (:region = 'GLOBAL' OR EXISTS (
                    SELECT 1 FROM item_regions r WHERE r.item_id = i.id AND r.region = :region))
        )
        SELECT id, title, url, source_id, region, round(eng::numeric, 2) AS eng,
               percent_rank() OVER (PARTITION BY source_id ORDER BY eng) AS pr
        FROM items ORDER BY pr DESC, eng DESC LIMIT :limit
        """,
        region=region,
        start=start,
        end=end,
        limit=limit,
    )


def keyword_growth(
    session: Session,
    region: str,
    start: datetime,
    end: datetime,
    z_threshold: float,
    limit: int = 10,
) -> list[dict]:
    return _rows(
        session,
        """
        SELECT key, round(max(score)::numeric, 2) AS score, round(max(z)::numeric, 2) AS z,
               round(sum(mentions)::numeric, 1) AS mentions, max(sources_count) AS sources
        FROM keyword_hourly_stats
        WHERE region = :region AND hour >= :start AND hour < :end
        GROUP BY key HAVING max(z) >= :thr
        ORDER BY max(score) DESC LIMIT :limit
        """,
        region=region,
        start=start,
        end=end,
        thr=z_threshold,
        limit=limit,
    )


# ---------------------------------------------------------------- 话题


def topic_ranking(
    session: Session,
    region: str,
    start: datetime,
    end: datetime,
    *,
    sort: str = "heat",
    limit: int = 10,
    categories: tuple[str, ...] | None = None,
    z_threshold: float | None = None,
) -> list[dict]:
    # 热度榜对“常驻”话题（首见早于窗口 2 天以上，如榜单上的常青应用）降权，突出当天新热点
    order = {
        "heat": "sum(s.engagement) * CASE WHEN t.first_seen >= CAST(:start AS timestamptz) - interval '2 days' "
        "THEN 1.0 ELSE 0.3 END DESC, sum(s.mentions) DESC",
        "growth": "max(s.score) DESC",
        "mentions": "sum(s.mentions) DESC",
    }[sort]
    having = "HAVING max(s.z) >= :thr" if z_threshold is not None else ""
    cat_filter = "AND t.category = ANY(:cats)" if categories else ""
    return _rows(
        session,
        f"""
        SELECT t.id, t.name, t.category, t.keywords, t.item_count, t.origin_region, t.active_regions,
               t.slow_variables, t.first_seen,
               round(sum(s.mentions)::numeric, 1) AS mentions,
               round(sum(s.engagement)::numeric, 1) AS engagement,
               round(max(s.z)::numeric, 2) AS max_z,
               round(max(s.score)::numeric, 2) AS max_score,
               max(s.sources_count) AS sources
        FROM topic_hourly_stats s JOIN topics t ON t.id = s.topic_id
        WHERE s.region = :region AND s.hour >= :start AND s.hour < :end AND t.status = 'active' {cat_filter}
        GROUP BY t.id {having}
        ORDER BY {order} LIMIT :limit
        """,
        region=region,
        start=start,
        end=end,
        limit=limit,
        thr=z_threshold,
        cats=list(categories or ()),
    )


def topic_detail(session: Session, topic_id: int, items_limit: int = 30) -> dict[str, Any] | None:
    topic = _rows(
        session,
        """
        SELECT id, name, summary, category, category_confidence, keywords, item_count, source_count,
               origin_region, active_regions, spread_path, slow_variables, status, merged_into,
               first_seen, last_seen
        FROM topics WHERE id = :id
        """,
        id=topic_id,
    )
    if not topic:
        return None
    detail = topic[0]
    detail["items"] = _rows(
        session,
        """
        SELECT i.id, i.title, i.url, i.source_id, i.region, i.country, i.language, i.fetched_at, i.metrics
        FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id
        WHERE ti.topic_id = :id ORDER BY i.fetched_at DESC LIMIT :limit
        """,
        id=topic_id,
        limit=items_limit,
    )
    detail["opportunities"] = opportunities(session, topic_id=topic_id, days=3650, limit=20)
    detail["validations"] = _rows(
        session,
        """
        SELECT DISTINCT ON (region, kind) region, kind, query, signal, payload, ts
        FROM validations WHERE topic_id = :id ORDER BY region, kind, ts DESC
        """,
        id=topic_id,
    )
    return detail


def topic_series(session: Session, topic_id: int, hours: int = 168) -> list[dict]:
    return _rows(
        session,
        """
        SELECT region, hour, mentions, engagement, z, score FROM topic_hourly_stats
        WHERE topic_id = :id AND hour >= now() - make_interval(hours => :hours)
        ORDER BY hour
        """,
        id=topic_id,
        hours=hours,
    )


def similar_topics(session: Session, topic_id: int, limit: int = 10) -> list[dict]:
    """pgvector 余弦近邻：这个话题历史上出现过吗？"""
    return _rows(
        session,
        """
        SELECT o.id, o.name, o.category, o.first_seen, o.last_seen, o.status,
               round((1 - (o.embedding <=> t.embedding))::numeric, 3) AS similarity
        FROM topics t JOIN topics o ON o.id <> t.id AND o.embedding IS NOT NULL
        WHERE t.id = :id AND t.embedding IS NOT NULL
        ORDER BY o.embedding <=> t.embedding LIMIT :limit
        """,
        id=topic_id,
        limit=limit,
    )


# ---------------------------------------------------------------- 商机


def opportunities(
    session: Session,
    *,
    region: str | None = None,
    topic_id: int | None = None,
    days: int = 1,
    limit: int = 20,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    """每个 (话题, 区域) 取最新一版深度分析，按排序模型分（无则 LLM 总分）降序。"""
    return _rows(
        session,
        """
        SELECT * FROM (
            SELECT DISTINCT ON (o.topic_id, o.region)
                   o.id, o.topic_id, t.name AS topic_name, t.category, o.region, o.model, o.summary,
                   o.scores, o.total_score, o.rank_score,
                   coalesce(o.rank_score, o.total_score) AS sort_score,
                   o.tags, o.why_now, o.distribution_path, o.product_shapes, o.counter_arguments,
                   o.known_players, o.durability, o.is_gap, o.gap_reason, o.created_at,
                   (SELECT f.label FROM feedback f WHERE f.opportunity_id = o.id
                    ORDER BY f.ts DESC LIMIT 1) AS feedback
            FROM opportunities o JOIN topics t ON t.id = o.topic_id
            WHERE (CAST(:region AS varchar) IS NULL OR o.region = :region)
              AND (CAST(:topic_id AS bigint) IS NULL OR o.topic_id = :topic_id)
              AND o.created_at >= coalesce(CAST(:since AS timestamptz),
                                           now() - make_interval(days => :days))
              AND o.created_at < coalesce(CAST(:until AS timestamptz), now() + interval '1 minute')
              AND o.stage = 'deep'
            ORDER BY o.topic_id, o.region, o.created_at DESC
        ) latest
        ORDER BY sort_score DESC NULLS LAST LIMIT :limit
        """,
        region=region,
        topic_id=topic_id,
        days=days,
        since=since,
        until=until,
        limit=limit,
    )


def evidence_for(session: Session, opportunity_id: int) -> list[dict]:
    return _rows(
        session,
        """
        SELECT e.kind, e.snippet, e.item_id, i.url, i.source_id
        FROM evidence e JOIN raw_items i ON i.id = e.item_id
        WHERE e.opportunity_id = :id ORDER BY e.kind, e.id
        """,
        id=opportunity_id,
    )


# ---------------------------------------------------------------- 跨区


def region_matrix(
    session: Session, regions: list[str], days: int = 7, limit: int = 15
) -> list[dict]:
    """行：近期活跃于 ≥2 个区域、或在某区域显著起飞的业务类话题；列：各区域近 72 小时最大 z。"""
    since = datetime.now().astimezone() - timedelta(days=days)
    rows = _rows(
        session,
        """
        WITH per AS (
            SELECT s.topic_id, s.region, max(s.z) AS max_z, sum(s.mentions) AS mentions
            FROM topic_hourly_stats s
            WHERE s.hour >= :since AND s.region <> 'GLOBAL' AND s.region = ANY(:regions)
            GROUP BY s.topic_id, s.region
        ), ranked AS (
            SELECT topic_id, count(*) FILTER (WHERE mentions >= 2) AS n_regions, max(max_z) AS best_z
            FROM per GROUP BY topic_id
        )
        SELECT t.id, t.name, t.category, t.spread_path, r.n_regions, r.best_z,
               jsonb_object_agg(p.region, round(p.max_z::numeric, 2)) AS z_by_region
        FROM ranked r JOIN topics t ON t.id = r.topic_id AND t.status = 'active'
        JOIN per p ON p.topic_id = r.topic_id
        WHERE t.category = ANY(:cats) AND (r.n_regions >= 2 OR r.best_z >= 2)
        GROUP BY t.id, r.n_regions, r.best_z
        ORDER BY r.n_regions DESC, r.best_z DESC NULLS LAST LIMIT :limit
        """,
        since=since,
        regions=regions,
        cats=list(BUSINESS_CATEGORIES),
        limit=limit,
    )
    return rows


def recent_spread(session: Session, days: int = 7, limit: int = 10) -> list[dict]:
    return _rows(
        session,
        """
        SELECT id, name, category, origin_region, active_regions, spread_path, last_seen
        FROM topics
        WHERE status = 'active' AND spread_path IS NOT NULL AND jsonb_array_length(spread_path) >= 2
          AND last_seen >= now() - make_interval(days => :days)
        ORDER BY last_seen DESC LIMIT :limit
        """,
        days=days,
        limit=limit,
    )


# ---------------------------------------------------------------- 运维


def source_health(session: Session, hours: int = 24) -> list[dict]:
    return _rows(
        session,
        """
        SELECT s.id, s.name, s.kind, s.default_region AS region, s.enabled, s.last_run_at, s.last_success_at,
               s.consecutive_failures, s.last_item_count, s.last_error,
               count(r.id) AS runs, count(r.id) FILTER (WHERE r.ok) AS ok_runs
        FROM sources s
        LEFT JOIN source_runs r ON r.source_id = s.id AND r.started_at >= now() - make_interval(hours => :hours)
        GROUP BY s.id ORDER BY s.enabled DESC, s.consecutive_failures DESC, s.id
        """,
        hours=hours,
    )


def llm_cost(session: Session, since: datetime, until: datetime) -> dict[str, Any]:
    row = _rows(
        session,
        """
        SELECT coalesce(sum(cost_usd), 0) AS cost_usd, count(*) AS calls,
               coalesce(sum(prompt_tokens + completion_tokens), 0) AS tokens
        FROM llm_usage WHERE ts >= :since AND ts < :until
        """,
        since=since,
        until=until,
    )
    return row[0]
