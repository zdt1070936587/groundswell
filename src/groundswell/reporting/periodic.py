"""周报 / 月报：窗口内的商机回顾、反馈与点子库进展、各区域最热话题、扩散路径、回测 KPI 与成本。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import queries
from groundswell.config import Settings, get_settings
from groundswell.configs import load_regions
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import local_day_window, now_utc
from groundswell.reporting.daily import md_escape
from groundswell.taxonomy import (
    BUSINESS_CATEGORIES,
    CATEGORY_LABELS,
    FEEDBACK_LABELS,
    IDEA_STATUS_LABELS,
)

Kind = Literal["weekly", "monthly"]
TITLES = {"weekly": "周报", "monthly": "月报"}
FEEDBACK_NAMES = dict(zip(FEEDBACK_LABELS, ("有价值", "一般", "无价值"), strict=True))


def period_window(kind: Kind, today: date, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    """上一个完整的自然周（周一至周日）/ 自然月，返回 (since, until, 标签)。"""
    if kind == "weekly":
        this_monday = today - timedelta(days=today.weekday())
        start_day = this_monday - timedelta(days=7)
        end_day = this_monday
        iso = start_day.isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
    else:
        first = today.replace(day=1)
        start_day = (first - timedelta(days=1)).replace(day=1)
        end_day = first
        label = f"{start_day:%Y-%m}"
    since, _ = local_day_window(tz, start_day)
    until, _ = local_day_window(tz, end_day)
    return since, until, label


def section_opportunities(session: Session, since: datetime, until: datetime) -> str:
    opps = queries.opportunities(session, since=since, until=until, limit=15)
    if not opps:
        return ""
    lines = [
        "## 本期商机 Top 15",
        "",
        "| # | 区域 | 话题 | 分数 | 反馈 | 摘要 |",
        "|---|---|---|---|---|---|",
    ]
    for i, o in enumerate(opps, 1):
        score = o["rank_score"] if o.get("rank_score") is not None else o["total_score"]
        tag = "·空白" if o.get("is_gap") else ""
        lines.append(
            f"| {i} | {o['region']}{tag} | {md_escape(o['topic_name'], 30)} | {score:.1f} | "
            f"{FEEDBACK_NAMES.get(o.get('feedback') or '', '—')} | {md_escape(o.get('summary'), 60)} |"
        )
    lines.append("")
    return "\n".join(lines)


def section_feedback(session: Session, since: datetime, until: datetime) -> str:
    counts = dict(
        session.execute(
            text("SELECT label, count(*) FROM feedback WHERE ts >= :s AND ts < :u GROUP BY label"),
            {"s": since, "u": until},
        ).all()
    )
    produced = session.execute(
        text(
            "SELECT count(*) FROM opportunities WHERE stage = 'deep' AND created_at >= :s AND created_at < :u"
        ),
        {"s": since, "u": until},
    ).scalar_one()
    total = sum(counts.values())
    lines = ["## 反馈与点子库", ""]
    ratio = f"{counts.get('good', 0) / total:.0%}" if total else "—"
    lines.append(
        f"- 产出商机 {produced} 条，反馈 {total} 条（"
        + "、".join(f"{FEEDBACK_NAMES[k]} {counts.get(k, 0)}" for k in FEEDBACK_LABELS)
        + f"），有价值比例 **{ratio}**（M3 目标 ≥ 30%）"
    )
    ideas = dict(session.execute(text("SELECT status, count(*) FROM ideas GROUP BY status")).all())
    moved = session.execute(
        text(
            """
            SELECT i.id, i.title, e.to_status FROM idea_events e JOIN ideas i ON i.id = e.idea_id
            WHERE e.ts >= :s AND e.ts < :u AND e.from_status IS NOT NULL ORDER BY e.ts DESC LIMIT 10
            """
        ),
        {"s": since, "u": until},
    ).all()
    if ideas:
        lines.append(
            "- 点子库："
            + "、".join(f"{IDEA_STATUS_LABELS[k]} {ideas.get(k, 0)}" for k in IDEA_STATUS_LABELS)
        )
    for idea_id, title, status in moved:
        lines.append(
            f"  - #{idea_id} {md_escape(title, 40)} → {IDEA_STATUS_LABELS.get(status, status)}"
        )
    lines.append("")
    return "\n".join(lines)


def section_regions(session: Session, settings: Settings, since: datetime, until: datetime) -> str:
    rfile = load_regions()
    lines = ["## 各区域本期最热话题", ""]
    any_rows = False
    for code in [c for c in settings.report_regions if c in rfile.regions]:
        rows = queries.topic_ranking(
            session, code, since, until, sort="mentions", limit=5, categories=BUSINESS_CATEGORIES
        )
        if not rows:
            continue
        any_rows = True
        items = "；".join(
            f"{md_escape(r['name'], 30)}（{CATEGORY_LABELS.get(r['category'], '—')}，提及 {r['mentions']:.0f}）"
            for r in rows
        )
        lines.append(f"- **{code}**：{items}")
    lines.append("")
    return "\n".join(lines) if any_rows else ""


def section_spread(session: Session, days: int) -> str:
    rows = queries.recent_spread(session, days=days, limit=10)
    if not rows:
        return ""
    lines = ["## 跨区扩散", ""]
    for r in rows:
        steps = " → ".join(str(p.get("region")) for p in r.get("spread_path") or [])
        lines.append(f"- {md_escape(r['name'], 40)}：{steps}")
    lines.append("")
    return "\n".join(lines)


def section_kpi(settings: Settings) -> str:
    path = Path(settings.reports_dir) / "backtest" / "kpi.jsonl"
    if not path.exists():
        return ""
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return ""
    k = json.loads(lines[-1])
    rate = f"{k['hit_rate']:.0%}" if k.get("hit_rate") is not None else "—"
    return (
        "## 回测 KPI\n\n"
        f"- 黄金集 {k['golden_version']}：命中率 {rate}，平均领先 {k.get('avg_lead_days') or '—'} 天，"
        f"有数据案例 {k['with_data']}/{k['cases']}（{k['generated_at'][:10]}）\n"
    )


def build_periodic(session: Session, kind: Kind, today: date | None = None) -> tuple[str, str]:
    settings = get_settings()
    tz = ZoneInfo(settings.report_timezone)
    today = today or now_utc().astimezone(tz).date()
    since, until, label = period_window(kind, today, tz)
    cost = queries.llm_cost(session, since, until)
    n_items = session.execute(
        text("SELECT count(*) FROM raw_items WHERE fetched_at >= :s AND fetched_at < :u"),
        {"s": since, "u": until},
    ).scalar_one()
    header = (
        f"# 观潮{TITLES[kind]} · {label}\n\n"
        f"> {since.astimezone(tz):%Y-%m-%d} ~ {(until - timedelta(seconds=1)).astimezone(tz):%Y-%m-%d} · "
        f"新增条目 {n_items} · LLM 调用 {cost['calls']} 次（约 ¥{float(cost['cost_usd']) * settings.usd_to_cny:.2f}）\n"
    )
    parts = [
        header,
        section_opportunities(session, since, until),
        section_feedback(session, since, until),
        section_regions(session, settings, since, until),
        section_spread(session, (until - since).days),
        section_kpi(settings),
        f"<sub>生成于 {now_utc().astimezone(tz):%Y-%m-%d %H:%M %Z} · Groundswell</sub>\n",
    ]
    return label, "\n".join(p for p in parts if p)


def build_and_save_periodic(
    kind: Kind, push: bool = False, today: date | None = None
) -> tuple[Path, str]:
    settings = get_settings()
    with session_scope() as s:
        label, md = build_periodic(s, kind, today)
    path = Path(settings.reports_dir) / kind / f"{label}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    logger.info(f"{TITLES[kind]}已写入 {path}")
    if push:
        from groundswell.reporting.dingtalk import send_markdown

        send_markdown(f"观潮{TITLES[kind]} {label}", md, settings=settings)
    return path, md
