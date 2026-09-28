"""Markdown 日报：商机 → 信息差矩阵 → 扩散路径 → 分区域热度/增长 → 订阅命中 → 源健康。

每个区块在上游数据不存在时自动降级（无话题 → 条目 + 关键词；无商机 → 省略），
所以 Phase 1 只有采集和关键词统计时也能出一份可读的日报。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import queries
from groundswell.config import Settings, get_settings
from groundswell.configs import RegionsFile, load_regions
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import local_day_window, now_utc
from groundswell.taxonomy import BUSINESS_CATEGORIES, CATEGORY_LABELS, DURABILITY_LABELS

VALIDATION_LABELS = {
    "gtrends_5y": "搜索趋势(5年)",
    "github": "GitHub 活跃",
    "appstore": "App Store 供给",
    "jobs": "招聘需求",
    "funding": "融资动态",
    "policy": "政策文件",
}


def md_escape(s: str | None, limit: int = 80) -> str:
    s = (s or "").replace("\n", " ").replace("|", "／").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def link(title: str | None, url: str | None, limit: int = 60) -> str:
    t = md_escape(title, limit).replace("[", "［").replace("]", "］")
    return f"[{t}]({url})" if url else t


def _fmt_list(values: Any, limit: int = 3) -> str:
    if not values:
        return "—"
    out = []
    for v in list(values)[:limit]:
        if isinstance(v, dict):
            v = v.get("name") or v.get("shape") or v.get("text") or next(iter(v.values()), "")
        out.append(md_escape(str(v), 60))
    return "；".join(out)


# ---------------------------------------------------------------- 区块


def _window(settings: Settings, regions: RegionsFile, region: str, day: date | None):
    if day is None:
        end = now_utc()
        return end - timedelta(hours=24), end
    return local_day_window(regions.timezone_of(region), day)


def section_opportunities(
    session: Session, since: datetime, until: datetime, limit: int = 8
) -> str:
    opps = queries.opportunities(session, since=since, until=until, limit=limit)
    if not opps:
        return ""
    lines = ["## 今日商机", ""]
    for i, o in enumerate(opps, 1):
        score = f"总分 {o['total_score']:.1f}"
        if o.get("rank_score") is not None:
            score += f" · 排序分 {o['rank_score']:.2f}"
        gap = " · **区域空白**" if o.get("is_gap") else ""
        lines.append(f"### {i}. [{o['region']}] {md_escape(o['topic_name'], 60)} — {score}{gap}")
        if o.get("summary"):
            lines.append(f"> {md_escape(o['summary'], 300)}")
        lines.append("")
        lines.append(f"- 为什么是现在：{md_escape(o.get('why_now'), 200) or '—'}")
        lines.append(f"- 分发路径：{md_escape(o.get('distribution_path'), 200) or '—'}")
        lines.append(f"- 产品形态：{_fmt_list(o.get('product_shapes'))}")
        lines.append(f"- 反方论点：{_fmt_list(o.get('counter_arguments'))}")
        lines.append(f"- 已知玩家：{_fmt_list(o.get('known_players'), 5)}")
        tags = " ".join(f"#{t}" for t in (o.get("tags") or [])[:6])
        durability = DURABILITY_LABELS.get(o.get("durability") or "unknown", "未知")
        lines.append(f"- 持续性：{durability}　标签：{tags or '—'}")
        ev = queries.evidence_for(session, o["id"])[:3]
        if ev:
            lines.append("- 证据：" + "；".join(link(e["snippet"], e["url"], 50) for e in ev))
        vals = _validations(session, o["topic_id"], o["region"])
        if vals:
            lines.append("- 验证：" + "；".join(vals))
        fb = f"（已反馈：{o['feedback']}）" if o.get("feedback") else ""
        lines.append(f"- 反馈：`gs feedback {o['id']} good|meh|bad`{fb}")
        lines.append("")
    return "\n".join(lines)


def _validations(session: Session, topic_id: int, region: str) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (kind) kind, signal, payload FROM validations
            WHERE topic_id = :t AND region = :r ORDER BY kind, ts DESC
            """
        ),
        {"t": topic_id, "r": region},
    ).all()
    out = []
    for kind, signal, payload in rows:
        label = VALIDATION_LABELS.get(kind, kind)
        summary = (payload or {}).get("summary")
        if summary:
            out.append(f"{label} {md_escape(summary, 60)}")
        elif signal is not None:
            out.append(f"{label} {signal:+.2f}")
    return out


def section_matrix(session: Session, regions: list[str]) -> str:
    rows = queries.region_matrix(session, [r for r in regions if r != "GLOBAL"], days=7, limit=12)
    if not rows:
        return ""
    cols = [r for r in regions if r != "GLOBAL"]
    lines = [
        "## 信息差矩阵（近 7 天各区域最大 Z 值）",
        "",
        "| 话题 | 类别 | " + " | ".join(cols) + " |",
        "|---|---|" + "---|" * len(cols),
    ]
    for r in rows:
        zs = r.get("z_by_region") or {}
        cells = []
        for c in cols:
            z = zs.get(c)
            if c not in zs:
                cells.append("·")
            elif z is None:
                cells.append("○")
            else:
                cells.append(f"**{z:.1f}**" if z >= 2 else f"{z:.1f}")
        lines.append(
            f"| {md_escape(r['name'], 30)} | {CATEGORY_LABELS.get(r['category'], '—')} | "
            + " | ".join(cells)
            + " |"
        )
    lines.append("")
    lines.append(
        "> 粗体 = 该区域已显著起飞（z ≥ 2）；“○” = 有讨论但历史不足以计算 z；“·” = 该区域尚无讨论，可能是空白。"
    )
    lines.append("")
    return "\n".join(lines)


def section_spread(session: Session) -> str:
    rows = queries.recent_spread(session, days=7, limit=8)
    if not rows:
        return ""
    lines = ["## 跨区扩散路径", ""]
    for r in rows:
        path = r.get("spread_path") or []
        steps = " → ".join(
            f"{p.get('region')}({str(p.get('first_breakout') or p.get('first_seen') or '')[5:16]})"
            for p in path
        )
        lines.append(f"- **{md_escape(r['name'], 40)}**：{steps}")
    lines.append("")
    return "\n".join(lines)


def section_region(
    session: Session, settings: Settings, regions: RegionsFile, code: str, day: date | None
) -> str:
    cfg = regions.regions.get(code)
    title = f"{code} · {cfg.name}" if cfg else code
    start, end = _window(settings, regions, code, day)
    tz = regions.timezone_of(code)
    lines = [
        f"### {title}",
        "",
        f"<sub>窗口 {start.astimezone(tz):%m-%d %H:%M} ~ {end.astimezone(tz):%m-%d %H:%M}（{tz.key}）</sub>",
        "",
    ]
    thr = settings.trend_z_threshold
    heat = queries.topic_ranking(session, code, start, end, sort="heat", limit=10)
    if heat:
        lines += ["**热度榜**", "", "| # | 话题 | 类别 | 提及 | 源数 |", "|---|---|---|---|---|"]
        for i, t in enumerate(heat, 1):
            lines.append(
                f"| {i} | [#{t['id']}] {md_escape(t['name'], 40)} | "
                f"{CATEGORY_LABELS.get(t['category'], '—')} | {t['mentions']} | {t['sources']} |"
            )
        growth = queries.topic_ranking(
            session,
            code,
            start,
            end,
            sort="growth",
            limit=10,
            z_threshold=thr,
            categories=BUSINESS_CATEGORIES,
        )
        lines += ["", f"**增长榜（业务类，z ≥ {thr:.1f}）**", ""]
        if growth:
            lines += ["| # | 话题 | 类别 | Z | 分数 |", "|---|---|---|---|---|"]
            for i, t in enumerate(growth, 1):
                lines.append(
                    f"| {i} | [#{t['id']}] {md_escape(t['name'], 40)} | "
                    f"{CATEGORY_LABELS.get(t['category'], '—')} | {t['max_z']} | {t['max_score']} |"
                )
        else:
            lines.append("暂无显著起飞的业务类话题。")
    else:
        items = queries.item_heat(session, code, start, end, limit=10)
        if not items:
            lines.append("该区域在窗口内暂无数据。")
            lines.append("")
            return "\n".join(lines)
        lines += ["**热度榜（条目级）**", ""]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {link(it['title'], it['url'])} <sub>{it['source_id']}</sub>")
        kws = queries.keyword_growth(session, code, start, end, thr, limit=12)
        lines += ["", f"**增长关键词（z ≥ {thr:.1f}）**", ""]
        if kws:
            lines.append("、".join(f"{k['key']}(z={k['z']})" for k in kws))
        else:
            lines.append("暂无（需积累至少一天的小时级基线）。")
    lines.append("")
    return "\n".join(lines)


def section_subscriptions(session: Session, since: datetime) -> str:
    rows = session.execute(
        text(
            """
            SELECT s.name, t.id, t.name, h.region, h.score
            FROM subscription_hits h JOIN subscriptions s ON s.id = h.subscription_id
            JOIN topics t ON t.id = h.topic_id
            WHERE h.ts >= :since ORDER BY s.name, h.score DESC
            """
        ),
        {"since": since},
    ).all()
    if not rows:
        return ""
    lines = ["## 订阅命中", ""]
    for sub, tid, tname, region, score in rows[:30]:
        lines.append(f"- 【{sub}】[{region}] #{tid} {md_escape(tname, 40)}（{score:.2f}）")
    lines.append("")
    return "\n".join(lines)


def section_health(session: Session) -> str:
    rows = [r for r in queries.source_health(session, hours=24) if r["enabled"]]
    bad = [r for r in rows if r["consecutive_failures"] > 0 or (r["runs"] and not r["ok_runs"])]
    stale = [
        r
        for r in rows
        if r["last_success_at"] and now_utc() - r["last_success_at"] > timedelta(hours=24)
    ]
    lines = [
        "## 数据源健康",
        "",
        f"启用 {len(rows)} 个源，24 小时内异常 {len(bad)} 个，超 24 小时无成功采集 {len(stale)} 个。",
    ]
    for r in bad[:10]:
        lines.append(
            f"- `{r['id']}` 连续失败 {r['consecutive_failures']} 次："
            f"{md_escape(r['last_error'], 100) or '—'}"
        )
    lines.append("")
    return "\n".join(lines)


def _header(
    session: Session, settings: Settings, title_day: str, since: datetime, until: datetime
) -> str:
    n_items = session.execute(
        text("SELECT count(*) FROM raw_items WHERE fetched_at >= :s AND fetched_at < :u"),
        {"s": since, "u": until},
    ).scalar_one()
    n_topics = session.execute(
        text("SELECT count(*) FROM topics WHERE status = 'active' AND last_seen >= :s"),
        {"s": since},
    ).scalar_one()
    cost = queries.llm_cost(session, since, until)
    cny = float(cost["cost_usd"]) * settings.usd_to_cny
    return "\n".join(
        [
            f"# 观潮日报 · {title_day}",
            "",
            f"> 新增条目 {n_items} · 活跃话题 {n_topics} · LLM 调用 {cost['calls']} 次"
            f"（约 ¥{cny:.2f}）",
            "",
        ]
    )


# ---------------------------------------------------------------- 入口


def build_daily(
    session: Session,
    day: date | None = None,
    regions: list[str] | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    rfile = load_regions()
    codes = [c for c in (regions or settings.report_regions) if c in rfile.regions]
    tz = ZoneInfo(settings.report_timezone)
    if day is None:
        until = now_utc()
        since = until - timedelta(hours=24)
        title_day = f"{until.astimezone(tz):%Y-%m-%d %H:%M}"
    else:
        since, until = local_day_window(tz, day)
        title_day = day.isoformat()

    parts = [_header(session, settings, title_day, since, until)]
    parts.append(section_opportunities(session, since, until))
    parts.append(section_matrix(session, codes))
    parts.append(section_spread(session))
    parts.append("## 分区域热点\n")
    for code in codes:
        parts.append(section_region(session, settings, rfile, code, day))
    parts.append(section_subscriptions(session, since))
    parts.append(section_health(session))
    parts.append(f"<sub>生成于 {now_utc().astimezone(tz):%Y-%m-%d %H:%M %Z} · Groundswell</sub>\n")
    return "\n".join(p for p in parts if p)


def build_and_save_daily(
    day: date | None = None, regions: list[str] | None = None, push: bool = False
) -> tuple[Path, str]:
    settings = get_settings()
    with session_scope() as s:
        md = build_daily(s, day=day, regions=regions, settings=settings)
    stamp = (
        day.isoformat()
        if day
        else f"{now_utc().astimezone(ZoneInfo(settings.report_timezone)):%Y-%m-%d}"
    )
    path = Path(settings.reports_dir) / "daily" / f"{stamp}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    logger.info(f"日报已写入 {path}")
    if push:
        from groundswell.reporting.dingtalk import send_markdown

        send_markdown(f"观潮日报 {stamp}", md, settings=settings)
    return path, md
