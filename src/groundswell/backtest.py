"""回测：系统能否在黄金集案例公认起飞日之前发出信号。

对每个案例，从 raw_items 中按关键词（拉丁词按词边界）统计目标区域的逐日提及数，
用与线上相同的 score_series / first_breakout 打分，记录首次突破日与领先天数。

- 只在起飞日前 MAX_LEAD_DAYS 天内找信号，更早的尖峰通常是同名词噪声（例如 cursor / threads 的日常用法）；
- approx 案例的起飞日有 ±2 周误差，允许信号晚到 TOLERANCE_DAYS 天仍算命中；
- 没有任何提及的案例记为 no_data，不计入命中率（先用 gs data import 导入历史数据）。

每次运行追加一行到 kpi.jsonl，命中率或平均领先天数低于上一次时标记 regression。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.config import get_settings
from groundswell.configs import (
    GLOBAL,
    GoldenCase,
    load_calendars,
    load_golden_set,
    load_methodology,
    load_regions,
)
from groundswell.db.session import session_scope
from groundswell.opportunity.prompts import PROMPT_VERSION
from groundswell.processing.seasonality import seasonal_factor
from groundswell.processing.terms import matches_any
from groundswell.processing.timeutil import now_utc
from groundswell.processing.trend import first_breakout, score_series

DAILY_WINDOW = 28
MIN_DAILY = 3.0
MAX_LEAD_DAYS = 60
TOLERANCE_DAYS = 14
AFTER_DAYS = 21


@dataclass
class CaseResult:
    name: str
    region: str
    breakout: date
    approx: bool
    status: str  # hit | late | miss | no_data
    signal_day: date | None = None
    lead_days: int | None = None
    mentions: float = 0.0
    peak_z: float | None = None


def evaluate_case(
    counts: list[float],
    start: date,
    case: GoldenCase,
    *,
    z_threshold: float,
    window: int = DAILY_WINDOW,
    min_count: float = MIN_DAILY,
    seasonal: list[float] | None = None,
) -> CaseResult:
    """counts[i] 为 start + i 天的提及数。"""
    result = CaseResult(case.name, case.region, case.breakout, case.approx, "no_data")
    result.mentions = round(float(sum(counts)), 1)
    if result.mentions == 0:
        return result
    points = score_series(counts, window=window, min_count=min_count, seasonal=seasonal)
    result.peak_z = max(p.z for p in points)
    earliest = max(0, (case.breakout - timedelta(days=MAX_LEAD_DAYS) - start).days)
    idx = first_breakout(points[earliest:], z_threshold)
    if idx is None:
        result.status = "miss"
        return result
    result.signal_day = start + timedelta(days=earliest + idx)
    result.lead_days = (case.breakout - result.signal_day).days
    tolerance = TOLERANCE_DAYS if case.approx else 0
    result.status = "hit" if result.lead_days >= -tolerance else "late"
    return result


def daily_counts(
    session: Session, case: GoldenCase, start: date, end: date, tz: Any
) -> list[float]:
    """[start, end) 内逐日提及数，按区域本地日期切分。"""
    start_ts = datetime.combine(start, time(), tzinfo=tz)
    end_ts = datetime.combine(end, time(), tzinfo=tz)
    patterns = [f"%{k}%" for k in case.keywords]
    rows = session.execute(
        text(
            """
            SELECT coalesce(i.published_at, i.fetched_at) AS ts, i.title, coalesce(i.text, '') AS body
            FROM raw_items i
            WHERE coalesce(i.published_at, i.fetched_at) >= :s AND coalesce(i.published_at, i.fetched_at) < :e
              AND (i.title ILIKE ANY(:p) OR i.text ILIKE ANY(:p))
              AND (:r = 'GLOBAL' OR i.region = :r
                   OR EXISTS (SELECT 1 FROM item_regions ir WHERE ir.item_id = i.id AND ir.region = :r))
            """
        ),
        {"s": start_ts, "e": end_ts, "p": patterns, "r": case.region},
    ).all()
    counts = [0.0] * (end - start).days
    for ts, title, body in rows:
        if not matches_any(f"{title} {body}", case.keywords):
            continue
        idx = (ts.astimezone(tz).date() - start).days
        if 0 <= idx < len(counts):
            counts[idx] += 1
    return counts


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    with_data = [r for r in results if r.status != "no_data"]
    hits = [r for r in with_data if r.status == "hit"]
    leads = [r.lead_days for r in hits if r.lead_days is not None]
    return {
        "cases": len(results),
        "with_data": len(with_data),
        "hits": len(hits),
        "hit_rate": round(len(hits) / len(with_data), 3) if with_data else None,
        "avg_lead_days": round(statistics.mean(leads), 1) if leads else None,
        "median_lead_days": statistics.median(leads) if leads else None,
    }


def _is_regression(current: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    if not previous or previous.get("golden_version") != current.get("golden_version"):
        return False
    for key in ("hit_rate", "avg_lead_days"):
        now, before = current.get(key), previous.get(key)
        if now is not None and before is not None and now < before:
            return True
    return False


def render(summary: dict[str, Any], results: list[CaseResult]) -> str:
    labels = {"hit": "✅ 命中", "late": "⏰ 偏晚", "miss": "❌ 未发现", "no_data": "· 无数据"}
    p = summary["params"]
    lines = [
        f"# 回测报告 {summary['generated_at'][:16]}",
        "",
        f"- 黄金集 {summary['golden_version']}，方法论 {summary['methodology_version']}，prompt {summary['prompt_version']}",
        f"- 参数：逐日窗口 {p['window']} 天，最小日提及 {p['min_count']}，z ≥ {p['z_threshold']}，"
        f"回看 {p['lookback_days']} 天，最早领先 {p['max_lead_days']} 天，约数案例容忍 {p['tolerance_days']} 天",
        f"- **命中率 {_fmt(summary['hit_rate'], pct=True)}**（{summary['hits']}/{summary['with_data']}，"
        f"有数据 {summary['with_data']}/{summary['cases']}），平均领先 {_fmt(summary['avg_lead_days'])} 天，"
        f"中位 {_fmt(summary['median_lead_days'])} 天",
    ]
    if summary.get("regression"):
        lines.append("- ⚠️ **KPI 低于上一次回测**，请检查最近的算法 / 参数改动")
    lines += [
        "",
        "| 案例 | 区域 | 起飞日 | 结果 | 首次信号 | 领先天数 | 提及 | 峰值 z |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.name} | {r.region} | {r.breakout}{'≈' if r.approx else ''} | {labels[r.status]} | "
            f"{r.signal_day or '—'} | {_fmt(r.lead_days)} | {r.mentions:.0f} | {_fmt(r.peak_z)} |"
        )
    if summary["with_data"] < summary["cases"]:
        lines += [
            "",
            "> 无数据的案例需要覆盖对应时间段的历史数据：`gs data import <file.jsonl> --source <id> --region <区域>`。",
        ]
    return "\n".join(lines) + "\n"


def _fmt(value: Any, pct: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:.0%}" if pct else str(value)


def run_backtest(
    golden_path: Path | None = None,
    lookback_days: int = 120,
    out_dir: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    settings = get_settings()
    golden = load_golden_set(golden_path)
    regions = load_regions()
    calendars = load_calendars()
    results = []
    with session_scope() as s:
        for case in golden.cases:
            tz = regions.timezone_of(case.region)
            start = case.breakout - timedelta(days=lookback_days)
            end = case.breakout + timedelta(days=AFTER_DAYS)
            counts = daily_counts(s, case, start, end, tz)
            region = case.region if case.region in regions.regions else GLOBAL
            seasonal = [
                seasonal_factor(
                    region,
                    datetime.combine(start + timedelta(days=i), time(12), tzinfo=tz),
                    calendars,
                    regions,
                )
                for i in range(len(counts))
            ]
            results.append(
                evaluate_case(
                    counts, start, case, z_threshold=settings.trend_z_threshold, seasonal=seasonal
                )
            )
    summary: dict[str, Any] = {
        "generated_at": now_utc().isoformat(),
        "golden_version": golden.version,
        "methodology_version": load_methodology().version,
        "prompt_version": PROMPT_VERSION,
        "params": {
            "window": DAILY_WINDOW,
            "min_count": MIN_DAILY,
            "z_threshold": settings.trend_z_threshold,
            "lookback_days": lookback_days,
            "max_lead_days": MAX_LEAD_DAYS,
            "tolerance_days": TOLERANCE_DAYS,
        },
        **summarize(results),
    }
    out_dir = Path(out_dir or settings.reports_dir / "backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    kpi_path = out_dir / "kpi.jsonl"
    previous = None
    if kpi_path.exists():
        lines = [ln for ln in kpi_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        previous = json.loads(lines[-1]) if lines else None
    summary["regression"] = _is_regression(summary, previous)
    stamp = now_utc().strftime("%Y%m%d-%H%M")
    path = out_dir / f"backtest-{stamp}.md"
    path.write_text(render(summary, results), encoding="utf-8")
    detail = {**summary, "results": [asdict(r) for r in results]}
    (out_dir / f"backtest-{stamp}.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    kpi = {k: summary[k] for k in ("generated_at", "golden_version", "hit_rate", "avg_lead_days")}
    kpi.update(with_data=summary["with_data"], cases=summary["cases"])
    with kpi_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(kpi, ensure_ascii=False) + "\n")
    return path, summary
