"""话题命名：名称（≤20 字中文）、一句话摘要、类别。

有 LLM 时批量（每批 10 个）交给便宜模型；否则取最具代表性的标题 + 关键词作为名称与摘要。
按近 24 小时提及量排序，优先命名正在热的话题；已命名话题在有新进展（12 小时后）时重新命名。
"""

from __future__ import annotations

from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import llm
from groundswell.processing.classify import _source_hints, classify_text
from groundswell.processing.cluster import representative_titles
from groundswell.processing.timeutil import now_utc
from groundswell.taxonomy import CATEGORIES

BATCH = 10
PROMPT_VERSION = "naming-v1"

SYSTEM = (
    "你是多语种互联网舆情编辑。你会看到若干个话题，每个话题包含几条来自不同平台的代表标题。"
    "为每个话题起一个准确、具体、不夸张的中文名称（专有名词保留原文），写一句话摘要，并归入一个类别。"
)

CATEGORY_GUIDE = (
    "类别只能是：entertainment（明星/影视/综艺/游戏八卦）、society（社会事件/事故/体育赛事/天气）、"
    "demand（用户在抱怨/求助/寻找替代品，暴露未被满足的需求）、tech（技术/AI/开发者/硬科技）、"
    "consumer（消费品/品牌/零售/出行/生活方式）、policy（政策/监管/法规/宏观）。"
)


class NamedTopic(BaseModel):
    id: int
    name: str = Field(min_length=1, max_length=60)
    summary: str = Field(default="", max_length=400)
    category: str
    confidence: float = Field(default=0.6, ge=0, le=1)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in CATEGORIES:
            raise ValueError(f"category 必须是 {CATEGORIES} 之一")
        return v


class NamingResult(BaseModel):
    topics: list[NamedTopic]


def _candidates(session: Session, limit: int) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT t.id, t.name, t.keywords, t.item_count, coalesce(h.m, 0) AS recent
            FROM topics t
            LEFT JOIN (
                SELECT topic_id, sum(mentions) AS m FROM topic_hourly_stats
                WHERE region = 'GLOBAL' AND hour >= now() - interval '24 hours' GROUP BY topic_id
            ) h ON h.topic_id = t.id
            WHERE t.status = 'active' AND t.last_seen >= now() - interval '2 days'
              AND (t.named_at IS NULL OR t.named_at < t.last_seen - interval '12 hours')
            ORDER BY coalesce(h.m, 0) DESC, t.item_count DESC, t.id DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    return [dict(r._mapping) for r in rows]


def heuristic_name(titles: list[str], keywords: list[str], item_count: int) -> tuple[str, str]:
    if not titles:
        return ("、".join(keywords[:3]) or "未命名话题"), ""
    # 代表标题：优先包含最多高频关键词、长度适中的标题
    kws = [k.lower() for k in keywords[:6]]

    def score(t: str) -> tuple[int, int]:
        low = t.lower()
        return (sum(k in low for k in kws), -abs(len(t) - 30))

    best = max(titles, key=score)
    name = best if len(best) <= 60 else best[:59] + "…"
    summary = f"{item_count} 条相关内容；关键词：{'、'.join(keywords[:6]) or '—'}"
    return name, summary


def _save(
    session: Session, tid: int, name: str, summary: str, category: str | None, conf: float
) -> None:
    now = now_utc()
    session.execute(
        text(
            """
            UPDATE topics SET name = :n, summary = :s, category = coalesce(:c, category),
                   category_confidence = CASE WHEN :c IS NULL THEN category_confidence ELSE :conf END,
                   named_at = :ts, classified_at = CASE WHEN :c IS NULL THEN classified_at ELSE :ts END
            WHERE id = :id
            """
        ),
        {"id": tid, "n": name[:200], "s": summary, "c": category, "conf": conf, "ts": now},
    )


def _llm_batch(batch: list[dict]) -> dict[int, NamedTopic]:
    blocks = []
    for t in batch:
        titles = "\n".join(f"  - {x}" for x in t["titles"][:8])
        blocks.append(f"[id={t['id']}] 关键词：{', '.join(t['keywords'][:8])}\n{titles}")
    prompt = (
        CATEGORY_GUIDE
        + "\n\n"
        + "\n\n".join(blocks)
        + '\n\n输出 JSON：{"topics": [{"id": 整数, "name": "≤20字", "summary": "≤60字", '
        '"category": "...", "confidence": 0~1}]}，每个 id 都要输出。'
    )
    result = llm.complete_json(
        prompt, system=SYSTEM, schema=NamingResult, stage="naming", max_tokens=2500
    )
    return {t.id: t for t in result.topics}


def name_topics(session: Session, limit: int = 100) -> int:
    cands = _candidates(session, limit)
    if not cands:
        return 0
    for c in cands:
        c["titles"] = representative_titles(session, c["id"], limit=8)
        c["keywords"] = list(c["keywords"] or [])
    use_llm = llm.available("cheap")
    hints = _source_hints()
    named = 0
    for start in range(0, len(cands), BATCH):
        batch = cands[start : start + BATCH]
        results: dict[int, NamedTopic] = {}
        if use_llm:
            try:
                results = _llm_batch(batch)
            except llm.BudgetExceeded as exc:
                logger.warning(f"{exc}，剩余话题改用启发式命名")
                use_llm = False
            except llm.LLMError as exc:
                logger.warning(f"LLM 命名失败，本批改用启发式：{exc}")
        for t in batch:
            r = results.get(t["id"])
            if r:
                _save(session, t["id"], r.name, r.summary, r.category, r.confidence)
            else:
                name, summary = heuristic_name(t["titles"], t["keywords"], t["item_count"])
                sources = session.execute(
                    text(
                        "SELECT DISTINCT i.source_id FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id "
                        "WHERE ti.topic_id = :t"
                    ),
                    {"t": t["id"]},
                ).scalars()
                cat, conf = classify_text(
                    " ".join([name, " ".join(t["keywords"]), " ".join(t["titles"])]),
                    [hints[s] for s in sources if s in hints],
                )
                _save(session, t["id"], name, summary, cat, conf)
            named += 1
    return named
