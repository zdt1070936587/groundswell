"""热度与增速。

纯函数部分（trend_point / score_series）与数据库无关，回测与线上共用同一套算法。
数据库部分把快照聚合成 (key|topic, region, hour) 的小时序列，再逐点打分。

- mentions：该小时被观测到的不同条目数（按区域权重累加）；
- z：相对过去 window 小时的突发程度，标准差下限取 sqrt(mean)（Poisson 近似），防止小基数暴涨；
- growth：相对 EWMA 基线的增速；
- score = seasonal × (0.4·z + 0.3·log1p(growth⁺)) + 0.3·log1p(mentions)。
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.configs import CalendarsFile, RegionsFile
from groundswell.processing.seasonality import seasonal_factor
from groundswell.processing.timeutil import floor_hour, hour_range

Z_CLIP = (-5.0, 10.0)


@dataclass(frozen=True)
class TrendPoint:
    z: float
    growth: float
    heat: float
    score: float
    low_sample: bool


def ewma(values: Sequence[float], alpha: float = 0.3) -> float:
    base = 0.0
    for i, v in enumerate(values):
        base = v if i == 0 else alpha * v + (1 - alpha) * base
    return base


def trend_point(
    history: Sequence[float],
    now: float,
    *,
    min_count: float = 3.0,
    alpha: float = 0.3,
    seasonal: float = 1.0,
) -> TrendPoint:
    hist = np.asarray(history, dtype=float)
    mean = float(hist.mean()) if hist.size else 0.0
    std = float(hist.std()) if hist.size else 0.0
    std_eff = max(std, math.sqrt(max(mean, 1.0)))
    low = now < min_count
    z = 0.0 if low else float(np.clip((now - mean) / std_eff, *Z_CLIP))
    base = ewma(hist.tolist(), alpha) if hist.size else 0.0
    growth = 0.0 if low else (now - base) / (base + 1.0)
    heat = math.log1p(max(now, 0.0))
    score = seasonal * (0.4 * z + 0.3 * math.log1p(max(growth, 0.0))) + 0.3 * heat
    return TrendPoint(round(z, 4), round(growth, 4), round(heat, 4), round(score, 4), low)


def score_series(
    series: Sequence[float],
    window: int = 48,
    min_count: float = 3.0,
    seasonal: Sequence[float] | None = None,
) -> list[TrendPoint]:
    """对等间隔序列逐点打分；每个点只使用它之前的 window 个点作为基线。"""
    out = []
    for i, now in enumerate(series):
        hist = series[max(0, i - window) : i]
        s = seasonal[i] if seasonal is not None else 1.0
        out.append(trend_point(hist, now, min_count=min_count, seasonal=s))
    return out


def first_breakout(points: Sequence[TrendPoint], z_threshold: float) -> int | None:
    for i, p in enumerate(points):
        if not p.low_sample and p.z >= z_threshold:
            return i
    return None


# ---------------------------------------------------------------- 数据库聚合

Kind = Literal["keyword", "topic"]

_ENGAGEMENT_SQL = """
    ln(1 + coalesce((i.metrics->>'points')::float, (i.metrics->>'score')::float,
                    (i.metrics->>'traffic')::float, (i.metrics->>'heat')::float,
                    (i.metrics->>'stars')::float, (i.metrics->>'reactions')::float, 0)
           + coalesce((i.metrics->>'comments')::float, 0))
    + CASE WHEN i.rank IS NOT NULL THEN greatest(0, 51 - i.rank) / 25.0 ELSE 0 END
"""

_AGG_SQL = {
    "keyword": """
        WITH obs AS (
            SELECT DISTINCT date_trunc('hour', s.ts) AS hour, s.item_id
            FROM item_snapshots s
            WHERE s.ts >= :start AND s.ts < :end
        ), base AS (
            SELECT o.hour, o.item_id, i.source_id, i.fetched_at, left(k.key, 128) AS key, 0.0 AS eng
            FROM obs o
            JOIN raw_items i ON i.id = o.item_id
            CROSS JOIN LATERAL unnest(i.keywords) AS k(key)
            WHERE i.keywords IS NOT NULL
        ), weighted AS (
            SELECT b.*, 'GLOBAL'::varchar AS region, 1.0::float AS weight FROM base b
            UNION ALL
            SELECT b.*, r.region, r.weight FROM base b JOIN item_regions r ON r.item_id = b.item_id
        )
        INSERT INTO keyword_hourly_stats (key, region, hour, mentions, new_items, sources_count)
        SELECT key, region, hour, sum(weight),
               sum(CASE WHEN date_trunc('hour', fetched_at) = hour THEN weight ELSE 0 END),
               count(DISTINCT source_id)
        FROM weighted
        GROUP BY key, region, hour
        ON CONFLICT (key, region, hour) DO UPDATE
        SET mentions = EXCLUDED.mentions, new_items = EXCLUDED.new_items,
            sources_count = EXCLUDED.sources_count
    """,
    "topic": f"""
        WITH obs AS (
            SELECT DISTINCT date_trunc('hour', s.ts) AS hour, s.item_id
            FROM item_snapshots s
            WHERE s.ts >= :start AND s.ts < :end
        ), base AS (
            SELECT o.hour, o.item_id, i.source_id, i.fetched_at, ti.topic_id AS key, {_ENGAGEMENT_SQL} AS eng
            FROM obs o
            JOIN raw_items i ON i.id = o.item_id
            JOIN topic_items ti ON ti.item_id = o.item_id
            JOIN topics t ON t.id = ti.topic_id AND t.status = 'active'
        ), weighted AS (
            SELECT b.*, 'GLOBAL'::varchar AS region, 1.0::float AS weight FROM base b
            UNION ALL
            SELECT b.*, r.region, r.weight FROM base b JOIN item_regions r ON r.item_id = b.item_id
        )
        INSERT INTO topic_hourly_stats (topic_id, region, hour, mentions, new_items, engagement, sources_count)
        SELECT key, region, hour, sum(weight),
               sum(CASE WHEN date_trunc('hour', fetched_at) = hour THEN weight ELSE 0 END),
               sum(eng * weight),
               count(DISTINCT source_id)
        FROM weighted
        GROUP BY key, region, hour
        ON CONFLICT (topic_id, region, hour) DO UPDATE
        SET mentions = EXCLUDED.mentions, new_items = EXCLUDED.new_items,
            engagement = EXCLUDED.engagement, sources_count = EXCLUDED.sources_count
    """,
}

_TABLE = {"keyword": ("keyword_hourly_stats", "key"), "topic": ("topic_hourly_stats", "topic_id")}


def aggregate_hours(session: Session, kind: Kind, start: datetime, end: datetime) -> int:
    start, end = floor_hour(start), floor_hour(end) + timedelta(hours=1)
    table, _ = _TABLE[kind]
    # 先清掉窗口内旧值（话题合并后，被合并话题的统计需要消失）
    session.execute(
        text(f"DELETE FROM {table} WHERE hour >= :start AND hour < :end"),
        {"start": start, "end": end},
    )
    result = session.execute(text(_AGG_SQL[kind]), {"start": start, "end": end})
    return result.rowcount or 0


def score_hours(
    session: Session,
    kind: Kind,
    start: datetime,
    end: datetime,
    *,
    window: int,
    min_count: float,
    calendars: CalendarsFile,
    regions: RegionsFile,
) -> int:
    """为 [start, end] 内的每个 (key, region, hour) 计算 z / growth / score。"""
    table, col = _TABLE[kind]
    start, end = floor_hour(start), floor_hour(end)
    hist_start = start - timedelta(hours=window)
    rows = session.execute(
        text(
            f"""
            SELECT {col}, region, hour, mentions FROM {table}
            WHERE hour >= :hs AND hour <= :end
              AND ({col}, region) IN (
                  SELECT DISTINCT {col}, region FROM {table}
                  WHERE hour >= :start AND hour <= :end AND mentions >= :minc
              )
            """
        ),
        {"hs": hist_start, "start": start, "end": end, "minc": min_count},
    ).all()
    series: dict[tuple, dict[datetime, float]] = defaultdict(dict)
    for key, region, hour, mentions in rows:
        series[(key, region)][hour] = float(mentions)
    # 冷启动保护：区域的观测历史不足半个窗口时，“新出现”不代表“在增长”，不给 z
    coverage = dict(
        session.execute(text(f"SELECT region, min(hour) FROM {table} GROUP BY region")).all()
    )
    min_history = timedelta(hours=max(1, window // 2))

    hours_all = hour_range(hist_start, end + timedelta(hours=1))
    targets = [h for h in hours_all if h >= start]
    factors: dict[tuple[str, datetime], float] = {}
    updates = []
    for (key, region), by_hour in series.items():
        values = [by_hour.get(h, 0.0) for h in hours_all]
        offset = len(hours_all) - len(targets)
        first = coverage.get(region)
        for j, hour in enumerate(targets):
            if hour not in by_hour:
                continue
            idx = offset + j
            if first is None or hour - first < min_history:
                heat = math.log1p(values[idx])
                updates.append(
                    {
                        "k": key,
                        "r": region,
                        "h": hour,
                        "z": None,
                        "g": None,
                        "s": round(0.3 * heat, 4),
                    }
                )
                continue
            fkey = (region, hour)
            if fkey not in factors:
                factors[fkey] = seasonal_factor(region, hour, calendars, regions)
            p = trend_point(
                values[max(0, idx - window) : idx],
                values[idx],
                min_count=min_count,
                seasonal=factors[fkey],
            )
            updates.append(
                {"k": key, "r": region, "h": hour, "z": p.z, "g": p.growth, "s": p.score}
            )
    if updates:
        session.execute(
            text(
                f"UPDATE {table} SET z = :z, growth = :g, score = :s WHERE {col} = :k AND region = :r AND hour = :h"
            ),
            updates,
        )
    return len(updates)
