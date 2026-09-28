"""差评挖掘：抓取 App Store 用户评论，把低星评论按痛点聚类，输出“别人家产品哪里让用户不爽”。

数据：iTunes 公开的 customer reviews RSS（每页 50 条，最多 10 页，按最新排序）。
聚类：配置了 LLM 时由便宜模型归纳痛点（引用必须来自原评论）；否则用痛点词典 + 关键词兜底。
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from groundswell import llm
from groundswell.config import Settings, get_settings
from groundswell.processing.keywords import extract_keywords
from groundswell.processing.lang import detect_language
from groundswell.processing.terms import count_matches
from groundswell.processing.timeutil import now_utc
from groundswell.reporting.daily import md_escape

REVIEWS_URL = "https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
LOOKUP_URL = "https://itunes.apple.com/lookup"
LLM_MAX_REVIEWS = 150

# 痛点 → (匹配词, 机会提示)
# fmt: off
PAIN_LEXICON: dict[str, tuple[list[str], str]] = {
    "闪退 / Bug": (
        ["crash", "crashes", "crashing", "bug", "bugs", "buggy", "glitch", "glitchy", "flicker",
         "flickering", "freeze", "freezes", "broken", "unusable", "not working", "doesn't work",
         "闪退", "崩溃", "卡死", "用不了"],
        "稳定性是基本盘：做一个“更少功能但绝不崩”的替代品",
    ),
    "付费 / 订阅 / 价格": (
        ["subscription", "subscribe", "price", "expensive", "paywall", "refund", "charged",
         "overpriced", "订阅", "收费", "扣费", "退款", "太贵", "会员", "续费"],
        "买断制、更便宜或按量计费的替代方案；或面向价格敏感人群的轻量版",
    ),
    "广告过多": (
        ["ads", "advert", "adverts", "advertisement", "advertising", "广告"],
        "无广告付费版 / 更克制的变现方式",
    ),
    "登录 / 账号": (
        ["login", "log in", "sign in", "account", "password", "verification",
         "登录", "账号", "验证码", "注册"],
        "免登录即用、更顺滑的账号体系",
    ),
    "性能 / 卡顿": (
        ["slow", "lag", "laggy", "loading", "battery", "drains", "卡顿", "很慢", "加载", "耗电", "发热"],
        "性能优先的轻量替代品",
    ),
    "同步 / 数据丢失": (
        ["sync", "syncing", "lost", "data loss", "backup", "deleted", "同步", "丢失", "备份", "数据没了"],
        "本地优先 / 可靠同步 / 数据导出工具",
    ),
    "客服无响应": (
        ["support", "customer service", "no response", "scam", "客服", "投诉"],
        "服务型切入：更好的客服与社群",
    ),
    "功能缺失": (
        ["feature", "missing", "wish", "please add", "would be nice", "can't", "cannot",
         "希望", "增加", "不支持", "没有"],
        "针对缺失功能做垂直插件 / 独立小工具",
    ),
    "更新后变差": (
        ["update", "new version", "latest version", "更新", "新版", "改版"],
        "抓住因改版流失的老用户",
    ),
    "难用 / 太复杂": (
        ["confusing", "complicated", "cluttered", "bloated", "hard to use", "learning curve",
         "overwhelming", "难用", "复杂", "看不懂"],
        "做减法：只保留核心场景的极简替代品",
    ),
}

# 关键词概括里没有信息量的词（应用名另行排除）
GENERIC_WORDS = {
    "app", "apps", "great", "good", "love", "like", "bad", "terrible", "awful", "please", "really",
    "just", "even", "much", "too", "still", "use", "using", "used", "one", "time", "get", "make",
    "应用", "软件", "真的", "就是", "什么", "一个", "可以", "没有",
}
# fmt: on


@dataclass
class Review:
    id: str
    rating: int
    title: str
    content: str
    version: str | None
    author: str | None

    @property
    def text(self) -> str:
        return f"{self.title} {self.content}".strip()


def parse_reviews(data: dict[str, Any]) -> list[Review]:
    entries = (data.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    out = []
    for e in entries:
        rating = (e.get("im:rating") or {}).get("label")
        if rating is None:  # 旧格式的第一条是应用信息
            continue
        out.append(
            Review(
                id=str((e.get("id") or {}).get("label") or len(out)),
                rating=int(rating),
                title=(e.get("title") or {}).get("label") or "",
                content=(e.get("content") or {}).get("label") or "",
                version=(e.get("im:version") or {}).get("label"),
                author=((e.get("author") or {}).get("name") or {}).get("label"),
            )
        )
    return out


def fetch_reviews(
    app_id: str, country: str, pages: int, client: httpx.Client
) -> tuple[str, list[Review]]:
    name = app_id
    try:
        info = client.get(LOOKUP_URL, params={"id": app_id, "country": country}).json()
        if info.get("results"):
            name = info["results"][0].get("trackName") or app_id
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"查询应用信息失败：{exc}")
    reviews: dict[str, Review] = {}
    for page in range(1, min(pages, 10) + 1):
        resp = client.get(REVIEWS_URL.format(country=country, page=page, app_id=app_id))
        if resp.status_code != 200:
            break
        batch = parse_reviews(resp.json())
        if not batch:
            break
        for r in batch:
            reviews.setdefault(r.id, r)
        time.sleep(0.5)
    return name, list(reviews.values())


# ---------------------------------------------------------------- 聚类


class PainPoint(BaseModel):
    name: str
    count: int = Field(ge=0)
    summary: str = ""
    opportunity: str = ""
    quotes: list[str] = []


class PainResult(BaseModel):
    pains: list[PainPoint]


def heuristic_pains(reviews: list[Review], app_name: str = "") -> list[PainPoint]:
    """每条评论归入匹配词最多的痛点；都不匹配的按关键词兜底。"""
    exclude = GENERIC_WORDS | set(re.findall(r"\w+", app_name.lower()))
    groups: dict[str, list[Review]] = {}
    rest: list[Review] = []
    for r in reviews:
        best, best_n = None, 0
        for pain, (terms, _) in PAIN_LEXICON.items():
            n = count_matches(r.text, terms)
            if n > best_n:
                best, best_n = pain, n
        if best:
            groups.setdefault(best, []).append(r)
        else:
            rest.append(r)
    pains = [
        PainPoint(
            name=pain,
            count=len(items),
            summary=_top_keywords(items, exclude),
            opportunity=PAIN_LEXICON[pain][1],
            quotes=[md_escape(r.text, 160) for r in items[:3]],
        )
        for pain, items in groups.items()
    ]
    if rest:
        pains.append(
            PainPoint(
                name="其他",
                count=len(rest),
                summary=_top_keywords(rest, exclude),
                quotes=[md_escape(r.text, 160) for r in rest[:3]],
            )
        )
    return sorted(pains, key=lambda p: -p.count)


def _top_keywords(items: list[Review], exclude: set[str], k: int = 6) -> str:
    counter: Counter[str] = Counter()
    for r in items:
        words = extract_keywords(r.title, r.content, detect_language(r.text))
        counter.update(
            {w for w in words if not any(t in exclude for t in re.findall(r"\w+", w.lower()))}
        )
    return "、".join(w for w, _ in counter.most_common(k))


_SYSTEM = (
    "你是用户研究员。只根据给出的评论归纳痛点，引用必须逐字摘自评论原文，不得编造。只输出 JSON。"
)

_PROMPT = """下面是应用「{name}」的 {n} 条低星评论（每行一条，[编号] 星级 标题｜正文）。
把它们归纳为 3–8 个痛点，按涉及评论数从多到少排列。

{reviews}

输出 JSON：{{"pains": [{{"name": "痛点名（≤12字）", "count": 涉及评论数, "summary": "≤60字概括",
"opportunity": "≤60字：针对该痛点的产品机会", "quotes": ["逐字引用 1–3 条"]}}]}}"""


def llm_pains(name: str, reviews: list[Review]) -> list[PainPoint]:
    sample = reviews[:LLM_MAX_REVIEWS]
    lines = "\n".join(
        f"[{i}] {r.rating}★ {md_escape(r.title, 60)}｜{md_escape(r.content, 300)}"
        for i, r in enumerate(sample)
    )
    result = llm.complete_json(
        _PROMPT.format(name=name, n=len(sample), reviews=lines),
        system=_SYSTEM,
        schema=PainResult,
        stage="reviews",
        max_tokens=2500,
    )
    corpus = " ".join(r.text for r in sample)
    for p in result.pains:
        p.quotes = [q for q in p.quotes if q and q[:20] in corpus][:3]
    return sorted(result.pains, key=lambda p: -p.count)


# ---------------------------------------------------------------- 报告


def render(
    name: str,
    app_id: str,
    country: str,
    reviews: list[Review],
    low: list[Review],
    pains: list[PainPoint],
    method: str,
) -> str:
    dist = Counter(r.rating for r in reviews)
    versions = Counter(r.version for r in low if r.version).most_common(3)
    lines = [
        f"# 差评挖掘 · {name}",
        "",
        f"> App Store {country.upper()} · id {app_id} · 抓取 {len(reviews)} 条评论，其中低星 {len(low)} 条 · "
        f"聚类方式：{method} · {now_utc():%Y-%m-%d}",
        "",
        "星级分布：" + " / ".join(f"{s}★ {dist.get(s, 0)}" for s in range(5, 0, -1)),
    ]
    if versions:
        lines.append("差评集中的版本：" + "、".join(f"{v}（{n}）" for v, n in versions))
    lines += [
        "",
        "## 痛点",
        "",
        "| 痛点 | 评论数 | 占比 | 概括 | 机会提示 |",
        "|---|---|---|---|---|",
    ]
    total = max(len(low), 1)
    for p in pains:
        lines.append(
            f"| {md_escape(p.name, 20)} | {p.count} | {p.count / total:.0%} | "
            f"{md_escape(p.summary, 60) or '—'} | {md_escape(p.opportunity, 60) or '—'} |"
        )
    lines += ["", "## 原话摘录", ""]
    for p in pains:
        if p.quotes:
            lines.append(f"**{md_escape(p.name, 20)}**")
            lines += [f"> {md_escape(q, 200)}" for q in p.quotes]
            lines.append("")
    return "\n".join(lines) + "\n"


def mine_app_reviews(
    app_id: str,
    country: str = "us",
    pages: int = 3,
    max_rating: int = 2,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> tuple[Path, str]:
    settings = settings or get_settings()
    own = client is None
    client = client or httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.http_proxy or None,
        follow_redirects=True,
    )
    try:
        name, reviews = fetch_reviews(app_id, country.lower(), pages, client)
    finally:
        if own:
            client.close()
    low = [r for r in reviews if r.rating <= max_rating]
    method = "词典 + 关键词"
    pains: list[PainPoint] = []
    if low and llm.available("cheap"):
        try:
            pains, method = llm_pains(name, low), f"LLM（{llm.model_for('cheap')}）"
        except llm.LLMError as exc:
            logger.warning(f"LLM 聚类失败，改用词典：{exc}")
    if low and not pains:
        pains = heuristic_pains(low, name)
    text = render(name, app_id, country, reviews, low, pains, method)
    path = (
        Path(settings.reports_dir) / "reviews" / f"{app_id}-{country.lower()}-{now_utc():%Y%m%d}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text
