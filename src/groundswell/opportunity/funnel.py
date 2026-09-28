"""商机三级漏斗（按区域分别运行）。

1. 候选（SQL 规则，零成本）：近 72 小时在该区域活跃的业务类话题，满足突发 / 多源 / 高提及之一；
   3 天内分析过的话题跳过。
2. 初筛（便宜模型批量；无 LLM 时启发式打分）：保留每区域得分最高的若干个。
3. 深度分析（强模型，每个 (话题, 区域) 一次）：注入区域上下文、方法论清单、增长数据、验证结果与证据，
   输出结构化分析。证据强制：引用的 item_id 必须来自提供的证据，有效证据 < 2 条即判定为无效分析。

预算熔断：每日 LLM 花费达到 GS_LLM_DAILY_BUDGET_CNY、或深度分析次数达到 GS_LLM_DEEP_DAILY_CAP 后，
剩余话题改用启发式分析（model = "heuristic"），日报照常产出。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import llm, queries
from groundswell.config import get_settings
from groundswell.configs import GLOBAL, Principle, RegionsFile, load_methodology, load_regions
from groundswell.db.models import Evidence, Opportunity
from groundswell.db.session import session_scope
from groundswell.opportunity.methodology import (
    match_signals,
    principles_text,
    sync_methodology,
    tag_weight,
)
from groundswell.opportunity.prompts import (
    DEEP_SYSTEM,
    DEEP_TEMPLATE,
    PROMPT_VERSION,
    SCREEN_SYSTEM,
    SCREEN_TEMPLATE,
    dimensions_text,
)
from groundswell.opportunity.schemas import (
    EvidenceRef,
    OpportunityAnalysis,
    ScreenResult,
    total_score,
)
from groundswell.processing.dedupe import normalize_title
from groundswell.processing.timeutil import now_utc
from groundswell.processing.trend import _ENGAGEMENT_SQL
from groundswell.taxonomy import BUSINESS_CATEGORIES, CATEGORY_LABELS

WINDOW_HOURS = 72
CANDIDATES_PER_REGION = 30
SCREEN_KEEP = 8
SCREEN_MIN = 5.0
SCREEN_BATCH = 15
EVIDENCE_ITEMS = 12
REANALYZE_DAYS = 3
MIN_EVIDENCE = 2


@dataclass
class Candidate:
    topic_id: int
    region: str
    name: str
    category: str | None
    summary: str | None
    keywords: list[str]
    slow_variables: list[str]
    origin_region: str | None
    active_regions: list[str]
    spread_path: list[Any] | None
    item_count: int
    mentions: float
    max_z: float
    max_score: float
    sources: int
    titles: list[str] = field(default_factory=list)
    screen_score: float = 0.0
    screen_reason: str = ""


# ---------------------------------------------------------------- 1. 候选


def select_candidates(
    session: Session, region: str, limit: int = CANDIDATES_PER_REGION
) -> list[Candidate]:
    settings = get_settings()
    rows = session.execute(
        text(
            """
            SELECT t.id AS topic_id, t.name, t.category, t.summary, t.keywords, t.slow_variables,
                   t.origin_region, t.active_regions, t.spread_path, t.item_count,
                   sum(s.mentions) AS mentions, coalesce(max(s.z), 0) AS max_z,
                   coalesce(max(s.score), 0) AS max_score, max(s.sources_count) AS sources
            FROM topic_hourly_stats s JOIN topics t ON t.id = s.topic_id
            WHERE s.region = :r AND s.hour >= now() - make_interval(hours => :h)
              AND t.status = 'active' AND t.category = ANY(:cats)
              AND NOT EXISTS (
                  SELECT 1 FROM opportunities o WHERE o.topic_id = t.id AND o.region = :r
                    AND o.created_at >= now() - make_interval(days => :days))
            GROUP BY t.id
            HAVING coalesce(max(s.z), 0) >= :thr OR max(s.sources_count) >= 2 OR sum(s.mentions) >= :minm
            ORDER BY max(s.score) DESC NULLS LAST, sum(s.mentions) DESC
            LIMIT :limit
            """
        ),
        {
            "r": region,
            "h": WINDOW_HOURS,
            "cats": list(BUSINESS_CATEGORIES),
            "days": REANALYZE_DAYS,
            "thr": settings.trend_z_threshold,
            "minm": settings.trend_min_count * 4,
            "limit": limit,
        },
    ).all()
    cands = [Candidate(region=region, **{k: v for k, v in r._mapping.items()}) for r in rows]
    for c in cands:
        c.keywords = list(c.keywords or [])
        c.slow_variables = list(c.slow_variables or [])
        c.active_regions = list(c.active_regions or [])
        c.titles = list(
            session.execute(
                text(
                    """
                    SELECT i.title FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id
                    WHERE ti.topic_id = :t ORDER BY i.id DESC LIMIT 5
                    """
                ),
                {"t": c.topic_id},
            ).scalars()
        )
    return cands


# ---------------------------------------------------------------- 2. 初筛


def heuristic_screen(c: Candidate, principles: list[Principle]) -> float:
    body = " ".join([c.name, " ".join(c.keywords), " ".join(c.titles)])
    s = 2.5
    s += min(max(c.max_z, 0.0), 5.0) * 0.8
    s += min(c.sources or 0, 4) * 0.5
    s += min(math.log1p(max(c.mentions or 0.0, 0.0)), 2.0)
    s += {"demand": 1.5, "tech": 0.5, "consumer": 0.5, "policy": 0.3}.get(c.category or "", 0.0)
    s += 0.5 if c.slow_variables else 0.0
    s += 0.5 * min(len(match_signals(body, principles)), 3)
    return round(min(s, 10.0), 2)


def _llm_screen(
    region: str, region_name: str, cands: list[Candidate]
) -> dict[int, tuple[bool, float, str]]:
    blocks = []
    for c in cands:
        titles = "；".join(t[:80] for t in c.titles[:4])
        blocks.append(
            f"[id={c.topic_id}] {c.name}｜{CATEGORY_LABELS.get(c.category, '—')}｜关键词 {', '.join(c.keywords[:6])}"
            f"｜z={c.max_z:.1f} 提及={c.mentions:.0f} 源数={c.sources}\n  代表标题：{titles}"
        )
    prompt = SCREEN_TEMPLATE.format(
        region=region, region_name=region_name, topics="\n".join(blocks)
    )
    result = llm.complete_json(
        prompt, system=SCREEN_SYSTEM, schema=ScreenResult, stage="screen", max_tokens=2000
    )
    return {t.id: (t.keep, t.score, t.reason) for t in result.topics}


def screen(
    region: str, region_name: str, cands: list[Candidate], principles: list[Principle]
) -> list[Candidate]:
    use_llm = llm.available("cheap")
    for start in range(0, len(cands), SCREEN_BATCH):
        batch = cands[start : start + SCREEN_BATCH]
        results: dict[int, tuple[bool, float, str]] = {}
        if use_llm:
            try:
                results = _llm_screen(region, region_name, batch)
            except llm.BudgetExceeded as exc:
                logger.warning(f"{exc}，初筛改用启发式")
                use_llm = False
            except llm.LLMError as exc:
                logger.warning(f"LLM 初筛失败，本批改用启发式：{exc}")
        for c in batch:
            if c.topic_id in results:
                keep, score, reason = results[c.topic_id]
                c.screen_score = score if keep else min(score, SCREEN_MIN - 0.1)
                c.screen_reason = reason
            else:
                c.screen_score = heuristic_screen(c, principles)
                c.screen_reason = "heuristic"
    kept = [c for c in cands if c.screen_score >= SCREEN_MIN]
    kept.sort(key=lambda c: -c.screen_score)
    return kept[:SCREEN_KEEP]


# ---------------------------------------------------------------- 3. 深度分析


@dataclass
class Context:
    evidence: list[dict]
    stats: list[dict]
    validations: list[dict]
    similar: list[dict]


def gather_context(session: Session, topic_id: int, region: str) -> Context:
    evidence = [
        dict(r._mapping)
        for r in session.execute(
            text(
                f"""
                SELECT i.id, i.title, left(coalesce(i.text, ''), 240) AS text, i.url, i.source_id, i.region,
                       i.country, {_ENGAGEMENT_SQL} AS eng,
                       (i.region = :r OR EXISTS (SELECT 1 FROM item_regions ir
                            WHERE ir.item_id = i.id AND ir.region = :r)) AS in_region
                FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id
                WHERE ti.topic_id = :t
                ORDER BY in_region DESC, (i.text IS NOT NULL) DESC, eng DESC, i.id DESC
                LIMIT :n
                """
            ),
            {"t": topic_id, "r": region, "n": EVIDENCE_ITEMS * 3},
        )
    ]
    # 同一内容在多个国家榜单/多个源重复出现时只保留一条
    seen: set[str] = set()
    unique = []
    for e in evidence:
        key = normalize_title(e["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    evidence = unique[:EVIDENCE_ITEMS]
    stats = [
        dict(r._mapping)
        for r in session.execute(
            text(
                """
                SELECT region, round(sum(mentions)::numeric, 1) AS mentions, round(max(z)::numeric, 2) AS max_z,
                       round(max(growth)::numeric, 2) AS max_growth, min(hour) FILTER (WHERE z >= 2) AS breakout
                FROM topic_hourly_stats
                WHERE topic_id = :t AND hour >= now() - make_interval(hours => :h)
                GROUP BY region ORDER BY sum(mentions) DESC
                """
            ),
            {"t": topic_id, "h": WINDOW_HOURS},
        )
    ]
    validations = [
        dict(r._mapping)
        for r in session.execute(
            text(
                """
                SELECT DISTINCT ON (kind) kind, query, signal, payload FROM validations
                WHERE topic_id = :t AND region = :r ORDER BY kind, ts DESC
                """
            ),
            {"t": topic_id, "r": region},
        )
    ]
    similar = queries.similar_topics(session, topic_id, limit=3)
    return Context(evidence, stats, validations, similar)


def input_hash(
    topic_id: int, region: str, evidence_ids: list[int], model: str, methodology_version: str
) -> str:
    raw = json.dumps(
        [topic_id, region, sorted(evidence_ids), PROMPT_VERSION, methodology_version, model],
        ensure_ascii=False,
    )
    return hashlib.sha1(raw.encode()).hexdigest()


def build_deep_prompt(
    c: Candidate, ctx: Context, regions: RegionsFile, principles: list[Principle]
) -> str:
    cfg = regions.regions.get(c.region)
    rc = cfg.context if cfg else None
    stats = "\n".join(
        f"- {s['region']}: 提及 {s['mentions']}，最大 z {s['max_z']}，最大增速 {s['max_growth']}，"
        f"首次突破 {s['breakout'] or '—'}"
        for s in ctx.stats
    )
    validations = "\n".join(
        f"- {v['kind']}（{v['query']}）：{(v['payload'] or {}).get('summary') or v['signal']}"
        for v in ctx.validations
    )
    evidence = "\n".join(
        f"- item_id={e['id']} [{e['source_id']}·{e['region'] or '?'}] {e['title'][:200]}"
        + (f"\n  正文：{e['text']}" if e["text"] else "")
        for e in ctx.evidence
    )
    similar = "；".join(
        f"{s['name']}（{str(s['first_seen'])[:10]}，相似度 {s['similarity']}）" for s in ctx.similar
    )
    spread = " → ".join(str(p.get("region")) for p in (c.spread_path or [])) or "—"
    return DEEP_TEMPLATE.format(
        region=c.region,
        region_name=cfg.name if cfg else c.region,
        channels="、".join(rc.channels) if rc and rc.channels else "—",
        payments="、".join(rc.payments) if rc and rc.payments else "—",
        regulation="、".join(rc.regulation) if rc and rc.regulation else "—",
        notes=(rc.notes if rc and rc.notes else "—"),
        name=c.name,
        category=CATEGORY_LABELS.get(c.category, "—"),
        summary=c.summary or "—",
        keywords="、".join(c.keywords[:10]) or "—",
        origin=c.origin_region or "—",
        active_regions="、".join(c.active_regions) or "—",
        spread=spread,
        slow_vars="、".join(c.slow_variables) or "—",
        similar=similar or "—",
        stats=stats or "—",
        validations=validations or "（尚无）",
        evidence=evidence or "（无）",
        principles=principles_text(principles),
        dimensions=dimensions_text(),
    )


_SHAPES = {
    "demand": [
        "针对该痛点的轻量 SaaS / 小程序，先做免费版收集用户",
        "社群 + 人工服务先验证付费意愿，再产品化",
    ],
    "tech": ["面向开发者的工具、插件或托管服务", "把该技术包装成垂直行业的开箱即用应用"],
    "consumer": ["选品 / 内容电商快速测款", "针对本地市场的替代品牌或本地化服务"],
    "policy": ["合规申报 / 资质办理工具", "政策解读与信息差服务（付费社群、咨询）"],
}


def heuristic_analysis(
    c: Candidate, ctx: Context, regions: RegionsFile, principles: list[Principle]
) -> OpportunityAnalysis:
    body = " ".join(
        [c.name, " ".join(c.keywords)] + [f"{e['title']} {e['text']}" for e in ctx.evidence]
    )
    matched = match_signals(body, principles)
    cfg = regions.regions.get(c.region)
    rc = cfg.context if cfg else None
    region_name = cfg.name if cfg else c.region
    demandish = {"unmet_demand", "schlep", "painkiller", "jtbd"} & {p.tag for p in matched}
    scores = {
        "demand": min(10.0, 4 + (2 if c.category == "demand" else 0) + 1.5 * len(demandish)),
        "growth": round(min(10.0, 3 + max(c.max_z, 0) * 1.2), 1),
        "willingness_to_pay": 4.0 + (1.5 if c.category in ("tech", "consumer") else 0),
        "competition_gap": 5.0,
        "feasibility": 6.0 if c.category in ("tech", "demand") else 5.0,
        "distribution": 6.0 if rc and rc.channels else 4.0,
        "durability": 7.0 if c.slow_variables else 4.0,
        "regional_fit": 6.0 if c.region != GLOBAL else 5.0,
    }
    top_region = max(ctx.stats, key=lambda s: s["mentions"] or 0) if ctx.stats else None
    why = [f"近 72 小时提及 {c.mentions:.0f} 次，最大 z={c.max_z:.1f}，覆盖 {c.sources} 个来源"]
    if top_region:
        why.append(f"讨论最集中的区域是 {top_region['region']}")
    if c.slow_variables:
        why.append(f"与长期趋势相关：{'、'.join(c.slow_variables)}")
    ev_kind = "demand" if c.category == "demand" else "growth"
    evidence = [
        EvidenceRef(item_id=e["id"], kind=ev_kind, quote=e["title"][:300])
        for e in ctx.evidence[:3]
        if e["title"]
    ]
    return OpportunityAnalysis(
        is_opportunity=True,
        summary=f"{region_name}：围绕「{c.name[:40]}」的{CATEGORY_LABELS.get(c.category, '')}类机会（启发式初判，待人工/LLM 复核）",
        scores=scores,
        tags=[p.tag for p in matched][:6],
        principles_applied=[p.id for p in matched][:6],
        why_now="；".join(why),
        distribution_path=("优先渠道：" + "、".join(rc.channels[:4])) if rc and rc.channels else "",
        product_shapes=_SHAPES.get(c.category or "", ["先用内容/社群验证需求"]),
        counter_arguments=[
            "启发式分析，未经 LLM 与人工核实",
            "热度可能只是一次性事件"
            if not c.slow_variables
            else "长期趋势明确，可能已有大量竞争者",
        ],
        known_players=[],
        durability="structural" if c.slow_variables else "unknown",
        evidence=evidence,
    )


def _save(
    session: Session,
    c: Candidate,
    analysis: OpportunityAnalysis,
    *,
    model: str,
    ihash: str,
    methodology_version: str,
    principles: list[Principle],
    evidence_ids: set[int],
    is_gap: bool = False,
    gap_reason: str | None = None,
) -> tuple[int, str]:
    valid = [e for e in analysis.evidence if e.item_id in evidence_ids]
    stage = "deep"
    if not analysis.is_opportunity:
        stage = "rejected"
    elif len(valid) < min(MIN_EVIDENCE, len(evidence_ids)):
        stage = "rejected"
        logger.info(f"#{c.topic_id}/{c.region} 有效证据 {len(valid)} 条，判定无效")
    base = total_score(analysis.scores)
    bonus = min(1.0, 0.1 * tag_weight(analysis.tags, principles))
    opp = Opportunity(
        topic_id=c.topic_id,
        region=c.region,
        input_hash=ihash,
        prompt_version=PROMPT_VERSION,
        methodology_version=methodology_version,
        model=model[:64],
        stage=stage,
        summary=analysis.summary,
        scores=analysis.scores,
        total_score=round(min(10.0, base + bonus), 2),
        tags=analysis.tags,
        why_now=analysis.why_now,
        distribution_path=analysis.distribution_path,
        product_shapes=analysis.product_shapes,
        counter_arguments=analysis.counter_arguments,
        known_players=analysis.known_players,
        durability=analysis.durability,
        is_gap=is_gap,
        gap_reason=gap_reason,
    )
    session.add(opp)
    session.flush()
    for e in valid:
        session.add(
            Evidence(
                opportunity_id=opp.id,
                item_id=e.item_id,
                kind=e.kind,
                snippet=e.quote,
                value={"principles": analysis.principles_applied},
            )
        )
    return opp.id, stage


def _deep_used_today(session: Session) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM opportunities WHERE model <> 'heuristic' AND created_at >= now() - interval '24 hours'"
        )
    ).scalar_one()


def analyze(
    c: Candidate,
    *,
    regions: RegionsFile,
    principles: list[Principle],
    methodology_version: str,
    use_llm: bool,
    is_gap: bool = False,
    gap_reason: str | None = None,
) -> tuple[int | None, str]:
    """分析单个 (话题, 区域)。返回 (opportunity_id, 状态)。"""
    with session_scope() as s:
        ctx = gather_context(s, c.topic_id, c.region)
    evidence_ids = {e["id"] for e in ctx.evidence}
    model = (llm.model_for("strong") or "heuristic") if use_llm else "heuristic"
    ihash = input_hash(c.topic_id, c.region, list(evidence_ids), model, methodology_version)
    with session_scope() as s:
        dup = s.execute(
            text(
                "SELECT id FROM opportunities WHERE topic_id = :t AND region = :r AND input_hash = :h"
            ),
            {"t": c.topic_id, "r": c.region, "h": ihash},
        ).first()
        if dup:
            return dup[0], "cached"
    analysis: OpportunityAnalysis | None = None
    status = "heuristic"
    if use_llm:
        prompt = build_deep_prompt(c, ctx, regions, principles)
        try:
            analysis = llm.complete_json(
                prompt,
                system=DEEP_SYSTEM,
                schema=OpportunityAnalysis,
                tier="strong",
                stage="deep",
                max_tokens=3000,
            )
            status = "llm"
        except llm.BudgetExceeded:
            raise
        except llm.LLMError as exc:
            logger.warning(f"深度分析失败 #{c.topic_id}/{c.region}，改用启发式：{exc}")
            model = "heuristic"
            ihash = input_hash(c.topic_id, c.region, list(evidence_ids), model, methodology_version)
    if analysis is None:
        analysis = heuristic_analysis(c, ctx, regions, principles)
    with session_scope() as s:
        opp_id, stage = _save(
            s,
            c,
            analysis,
            model=model,
            ihash=ihash,
            methodology_version=methodology_version,
            principles=principles,
            evidence_ids=evidence_ids,
            is_gap=is_gap,
            gap_reason=gap_reason,
        )
    return opp_id, (status if stage == "deep" else "rejected")


def run_funnel(regions: list[str] | None = None, deep_limit: int | None = None) -> dict[str, Any]:
    settings = get_settings()
    rfile = load_regions()
    methodology = load_methodology()
    principles = methodology.principles
    with session_scope() as s:
        version = sync_methodology(s, methodology)
        used = _deep_used_today(s)
    codes = [r for r in (regions or settings.report_regions or rfile.codes()) if r in rfile.regions]
    cap = settings.llm_deep_daily_cap if deep_limit is None else deep_limit
    remaining = max(0, cap - used)
    use_llm = llm.available("strong")
    stats: dict[str, Any] = {"llm": use_llm, "deep_budget": remaining, "regions": {}}
    started = now_utc()
    for code in codes:
        with session_scope() as s:
            cands = select_candidates(s, code)
        region_name = rfile.regions[code].name
        kept = screen(code, region_name, cands, principles)
        rs = {
            "candidates": len(cands),
            "screened": len(kept),
            "llm": 0,
            "heuristic": 0,
            "rejected": 0,
            "cached": 0,
        }
        for c in kept:
            llm_ok = use_llm and remaining > 0
            try:
                _, status = analyze(
                    c,
                    regions=rfile,
                    principles=principles,
                    methodology_version=version,
                    use_llm=llm_ok,
                )
            except llm.BudgetExceeded as exc:
                logger.warning(f"{exc}，剩余商机改用启发式")
                use_llm = False
                _, status = analyze(
                    c,
                    regions=rfile,
                    principles=principles,
                    methodology_version=version,
                    use_llm=False,
                )
            if status == "llm":
                remaining -= 1
            rs[status] = rs.get(status, 0) + 1
        stats["regions"][code] = rs
    stats["seconds"] = round((now_utc() - started).total_seconds(), 1)
    logger.info(f"商机漏斗完成：{stats}")
    return stats


def candidate_for(session: Session, topic_id: int, region: str) -> Candidate | None:
    """为指定 (话题, 区域) 构造候选（区域空白分析用：目标区域本身可能没有统计数据）。"""
    row = session.execute(
        text(
            """
            SELECT t.id AS topic_id, t.name, t.category, t.summary, t.keywords, t.slow_variables,
                   t.origin_region, t.active_regions, t.spread_path, t.item_count,
                   coalesce(sum(s.mentions), 0) AS mentions, coalesce(max(s.z), 0) AS max_z,
                   coalesce(max(s.score), 0) AS max_score, coalesce(max(s.sources_count), 0) AS sources
            FROM topics t LEFT JOIN topic_hourly_stats s
              ON s.topic_id = t.id AND s.hour >= now() - make_interval(hours => :h)
            WHERE t.id = :t GROUP BY t.id
            """
        ),
        {"t": topic_id, "h": WINDOW_HOURS},
    ).first()
    if not row:
        return None
    c = Candidate(region=region, **dict(row._mapping))
    c.keywords = list(c.keywords or [])
    c.slow_variables = list(c.slow_variables or [])
    c.active_regions = list(c.active_regions or [])
    return c
