"""单个商机的深度报告（Trends.vc 风格）+ 一页纸商业计划草稿。

报告部分完全来自已有数据（分析结果、证据、增长数据、验证、区域上下文），不额外调用 LLM；
一页纸计划在配置了强模型时由 LLM 基于这些材料起草，否则按分析字段套模板生成。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel
from sqlalchemy import text

from groundswell import llm, queries
from groundswell.config import get_settings
from groundswell.configs import load_regions
from groundswell.db.session import session_scope
from groundswell.opportunity.schemas import DIMENSIONS
from groundswell.processing.timeutil import now_utc
from groundswell.reporting.daily import VALIDATION_LABELS, link, md_escape
from groundswell.taxonomy import CATEGORY_LABELS, DURABILITY_LABELS

REVENUE_HINTS = {
    "demand": "SaaS 订阅（按席位 / 按用量），早期可用人工服务 + 年费验证付费意愿",
    "tech": "开源核心 + 托管 / 企业版；或按 API 调用计费",
    "consumer": "电商差价 / 品牌溢价；内容带货佣金",
    "policy": "按次服务费（申报、合规咨询）+ 会员制信息服务",
}


class OnePager(BaseModel):
    problem: str
    customer: str
    solution: str
    why_now: str
    channels: list[str]
    revenue_model: str
    key_metrics: list[str]
    validation_plan: list[str]
    risks: list[str]
    resources: str


_SYSTEM = (
    "你是创业教练。根据给定的商机分析与证据，为 2–5 人团队起草一页纸商业计划。"
    "只使用材料中的事实，不编造数据；验证计划要具体到第 1–4 周每周做什么。只输出 JSON。"
)


def _load(opportunity_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        rows = queries._rows(
            s,
            """
            SELECT o.*, t.name AS topic_name, t.category, t.keywords, t.spread_path, t.origin_region,
                   t.slow_variables, t.first_seen
            FROM opportunities o JOIN topics t ON t.id = o.topic_id WHERE o.id = :id
            """,
            id=opportunity_id,
        )
        if not rows:
            return None
        o = rows[0]
        o["evidence"] = queries.evidence_for(s, opportunity_id)
        o["validations"] = queries._rows(
            s,
            """
            SELECT DISTINCT ON (kind) kind, query, signal, payload FROM validations
            WHERE topic_id = :t AND region = :r ORDER BY kind, ts DESC
            """,
            t=o["topic_id"],
            r=o["region"],
        )
        o["stats"] = queries._rows(
            s,
            """
            SELECT region, round(sum(mentions)::numeric, 1) AS mentions, round(max(z)::numeric, 2) AS max_z,
                   min(hour) FILTER (WHERE z >= 2) AS breakout
            FROM topic_hourly_stats WHERE topic_id = :t AND hour >= :since
            GROUP BY region ORDER BY sum(mentions) DESC
            """,
            t=o["topic_id"],
            since=o["first_seen"],
        )
        o["feedback"] = s.execute(
            text("SELECT label, note FROM feedback WHERE opportunity_id = :id ORDER BY ts"),
            {"id": opportunity_id},
        ).all()
    return o


def _items(values: Any) -> list[str]:
    out = []
    for v in values or []:
        if isinstance(v, dict):
            v = (
                v.get("name")
                or v.get("shape")
                or v.get("text")
                or json.dumps(v, ensure_ascii=False)
            )
        out.append(str(v))
    return out


def heuristic_plan(o: dict[str, Any], channels: list[str]) -> OnePager:
    shapes = _items(o.get("product_shapes"))
    return OnePager(
        problem=o.get("summary") or o["topic_name"],
        customer=f"{o['region']} 区域中正在讨论「{o['topic_name']}」的用户（见证据中的原帖作者画像）",
        solution=shapes[0] if shapes else "先用内容 / 社群形式验证需求，再产品化",
        why_now=o.get("why_now") or "—",
        channels=channels[:4] or ["从证据来源的社区冷启动"],
        revenue_model=REVENUE_HINTS.get(o.get("category") or "", "先验证付费意愿，再定价"),
        key_metrics=[
            "落地页转化率（访问 → 留资）",
            "访谈中愿意付费的比例",
            "首批 10 个付费用户的留存",
        ],
        validation_plan=[
            "第 1 周：在证据来源的社区发帖 / 私信 20 人访谈，确认痛点与现有替代方案",
            "第 2 周：做落地页 + 等候名单，投放到主要渠道，目标留资 100",
            "第 3 周：手工 / 半自动交付给 5–10 个种子用户（Concierge MVP）",
            "第 4 周：尝试收费，记录付费转化与流失原因，决定继续 / 转向 / 放弃",
        ],
        risks=_items(o.get("counter_arguments"))[:3] or ["热度可能是一次性事件"],
        resources="2 人：1 人产品 / 开发，1 人内容 / 增长；首月预算以投放测试为主",
    )


def llm_plan(o: dict[str, Any], context: str) -> OnePager:
    material = {
        k: o.get(k)
        for k in (
            "topic_name",
            "region",
            "summary",
            "why_now",
            "distribution_path",
            "product_shapes",
            "counter_arguments",
            "known_players",
            "scores",
            "tags",
        )
    }
    material["evidence"] = [e["snippet"] for e in o["evidence"][:8]]
    prompt = (
        f"# 区域上下文\n{context}\n\n# 商机分析\n{json.dumps(material, ensure_ascii=False, default=str)}\n\n"
        "输出 JSON 字段：problem, customer, solution, why_now, channels(list), revenue_model, "
        "key_metrics(list), validation_plan(list，第 1–4 周), risks(list), resources"
    )
    return llm.complete_json(
        prompt, system=_SYSTEM, schema=OnePager, tier="strong", stage="brief", max_tokens=2000
    )


def render(o: dict[str, Any], plan: OnePager, plan_by: str, context_lines: list[str]) -> str:
    cat = CATEGORY_LABELS.get(o.get("category"), "—")
    gap = " · 区域空白" if o.get("is_gap") else ""
    lines = [
        f"# {md_escape(o['topic_name'], 80)}",
        "",
        f"> 商机 #{o['id']} · {o['region']} · {cat}{gap} · 总分 {o['total_score']:.1f}"
        + (f" · 排序分 {o['rank_score']:.2f}" if o.get("rank_score") is not None else "")
        + f" · 分析模型 {o['model']} · {o['created_at']:%Y-%m-%d}",
        "",
        f"**一句话**：{o.get('summary') or '—'}",
        "",
        "## 为什么是现在",
        "",
        o.get("why_now") or "—",
        "",
    ]
    if o["stats"]:
        lines += ["| 区域 | 提及 | 最大 z | 首次突破 |", "|---|---|---|---|"]
        for st in o["stats"]:
            z = st["max_z"] if st["max_z"] is not None else "—"
            breakout = f"{st['breakout']:%m-%d %H:%M}" if st["breakout"] else "—"
            lines.append(f"| {st['region']} | {st['mentions']} | {z} | {breakout} |")
        lines.append("")
    path = o.get("spread_path") or []
    if path:
        lines.append("扩散路径：" + " → ".join(str(p.get("region")) for p in path))
        lines.append("")
    if o.get("slow_variables"):
        lines += [f"长期趋势（慢变量）：{'、'.join(o['slow_variables'])}", ""]
    lines += ["## 信号与证据", ""]
    for e in o["evidence"]:
        lines.append(f"- [{e['kind']}] {link(e['snippet'], e['url'], 120)}（{e['source_id']}）")
    if o["validations"]:
        lines += ["", "**二次验证**", ""]
        for v in o["validations"]:
            payload = v["payload"] or {}
            label = VALIDATION_LABELS.get(v["kind"], v["kind"])
            lines.append(f"- {label}（{v['query']}）：{payload.get('summary') or v['signal']}")
    scores = o.get("scores") or {}
    lines += [
        "",
        "## 评分",
        "",
        "| 维度 | 分数 | 说明 |",
        "|---|---|---|",
        *[f"| {k} | {scores.get(k, '—')} | {desc} |" for k, (desc, _) in DIMENSIONS.items()],
        "",
        f"持续性判断：{DURABILITY_LABELS.get(o.get('durability') or 'unknown', '未知')}　"
        f"方法论标签：{' '.join('#' + t for t in o.get('tags') or []) or '—'}",
        "",
        "## 区域上下文",
        "",
        *context_lines,
        "",
        "## 产品形态与分发",
        "",
        *[f"- {md_escape(s, 200)}" for s in _items(o.get("product_shapes"))],
        f"- 分发路径：{o.get('distribution_path') or '—'}",
        "",
        "## 竞争格局",
        "",
        f"- 已知玩家：{'、'.join(_items(o.get('known_players'))) or '—'}",
    ]
    for v in o["validations"]:
        examples = (v["payload"] or {}).get("examples") or []
        if v["kind"] in ("appstore", "github") and examples:
            lines.append(
                f"- {VALIDATION_LABELS[v['kind']]}样本：" + "、".join(_items(examples)[:5])
            )
    lines += [
        "",
        "## 风险与反方论点",
        "",
        *[f"- {md_escape(c, 200)}" for c in _items(o.get("counter_arguments"))],
        "",
        f"## 一页纸商业计划（草稿，{plan_by}）",
        "",
        f"- **问题**：{plan.problem}",
        f"- **目标用户**：{plan.customer}",
        f"- **解决方案**：{plan.solution}",
        f"- **为什么是现在**：{plan.why_now}",
        f"- **渠道**：{'；'.join(plan.channels)}",
        f"- **收入模式**：{plan.revenue_model}",
        f"- **关键指标**：{'；'.join(plan.key_metrics)}",
        f"- **所需资源**：{plan.resources}",
        "",
        "**四周验证计划**",
        "",
        *[f"{i}. {step}" for i, step in enumerate(plan.validation_plan, 1)],
        "",
        "**主要风险**",
        "",
        *[f"- {r}" for r in plan.risks],
        "",
    ]
    if o["feedback"]:
        lines += [
            "## 人工反馈",
            "",
            *[f"- {label}：{note or ''}" for label, note in o["feedback"]],
            "",
        ]
    lines.append(f"<sub>生成于 {now_utc():%Y-%m-%d %H:%M} UTC · Groundswell</sub>")
    return "\n".join(lines) + "\n"


def build_brief(opportunity_id: int) -> tuple[Path, str]:
    o = _load(opportunity_id)
    if o is None:
        raise ValueError(f"商机 #{opportunity_id} 不存在")
    cfg = load_regions().regions.get(o["region"])
    rc = cfg.context if cfg else None
    context_lines = [
        f"- 区域：{o['region']}（{cfg.name if cfg else '—'}）",
        f"- 主要渠道：{'、'.join(rc.channels) if rc and rc.channels else '—'}",
        f"- 支付方式：{'、'.join(rc.payments) if rc and rc.payments else '—'}",
        f"- 监管要点：{'、'.join(rc.regulation) if rc and rc.regulation else '—'}",
    ]
    if rc and rc.notes:
        context_lines.append(f"- 备注：{rc.notes}")
    plan, plan_by = None, "模板生成"
    if llm.available("strong"):
        try:
            plan, plan_by = llm_plan(o, "\n".join(context_lines)), f"LLM {llm.model_for('strong')}"
        except llm.LLMError as exc:
            logger.warning(f"一页纸计划 LLM 生成失败，改用模板：{exc}")
    plan = plan or heuristic_plan(o, list(rc.channels) if rc else [])
    md = render(o, plan, plan_by, context_lines)
    path = Path(get_settings().reports_dir) / "brief" / f"opp-{opportunity_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return path, md
