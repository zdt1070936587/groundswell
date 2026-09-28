"""商机二次验证：用舆情之外的独立信号检验“热度”是否对应真实的需求 / 供给 / 资本动向。

| kind        | 数据源                         | signal 含义                              |
|-------------|--------------------------------|------------------------------------------|
| gtrends_5y  | Google Trends（pytrends）      | 近 12 个月均值 / 前 48 个月均值 - 1（长期斜率） |
| github      | GitHub Search API              | 近 90 天新建相关仓库数（log1p）            |
| appstore    | iTunes Search API              | 同类 App 供给密度（结果数 / 50）           |
| jobs        | HN “Who is hiring” 月帖        | 近 6 期招聘帖提及数（log1p）               |
| funding     | HN Algolia（raises / funding） | 近 180 天融资新闻数（log1p）               |
| policy      | 已采集的政策类源（本库）        | 近一年相关政策文件数（log1p）              |

每个 (话题, 区域, kind) 3 天内只验证一次。单个验证器失败不影响其他验证器。
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import text

from groundswell.config import Settings, get_settings
from groundswell.configs import GLOBAL, RegionsFile, load_regions
from groundswell.db.models import Validation
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import now_utc

CACHE_DAYS = 3
_LATIN = re.compile(r"^[a-z0-9][a-z0-9 .+\-]{1,40}$")
_HEAD_SPLIT = re.compile(r"\s*[:：|｜–—]\s*|\s+-\s+")

Result = tuple[float | None, dict[str, Any]]


def query_terms(name: str, keywords: list[str]) -> tuple[str, str | None]:
    """返回 (本地语言查询, 英文查询)。英文查询用于 GitHub / 招聘 / 融资等英文为主的数据源。

    “Temu: Shop Like a Billionaire” 这类“品牌 + 副标题”的名称优先取品牌部分。
    """
    head = _HEAD_SPLIT.split(name, maxsplit=1)[0].strip()
    if head != name.strip() and 2 <= len(head) <= 30:
        return head, (head if head.isascii() else None)
    latin = [k for k in keywords if _LATIN.match(k) and len(k) >= 3]
    en = " ".join(latin[:2]) if latin else (name if name.isascii() else None)
    local = keywords[0] if keywords else name
    return local[:60], (en[:60] if en else None)


def _client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.http_proxy or None,
        follow_redirects=True,
    )


# ---------------------------------------------------------------- 各验证器


def validate_gtrends(query: str, country: str | None, **_: Any) -> Result:
    try:
        from pytrends.request import TrendReq
    except ImportError:
        return None, {"skipped": "pytrends 未安装"}
    pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
    pt.build_payload([query], timeframe="today 5-y", geo=(country or "").upper())
    df = pt.interest_over_time()
    if df is None or df.empty or query not in df:
        return None, {"summary": "无搜索数据"}
    series = df[query].astype(float).tolist()
    recent, before = series[-52:], series[:-52]
    if not before or sum(before) == 0:
        return None, {
            "summary": "新词，无历史基线",
            "recent_mean": round(sum(recent) / max(len(recent), 1), 1),
        }
    ratio = (sum(recent) / len(recent)) / (sum(before) / len(before)) - 1
    return round(ratio, 3), {
        "summary": f"近一年搜索量较此前 {ratio:+.0%}",
        "recent_mean": round(sum(recent) / len(recent), 1),
        "peak": max(series),
    }


def validate_github(query: str, client: httpx.Client, settings: Settings, **_: Any) -> Result:
    from groundswell.collectors.github_trending import github_headers

    since = (now_utc() - timedelta(days=90)).date().isoformat()
    resp = client.get(
        "https://api.github.com/search/repositories",
        params={"q": f"{query} created:>{since}", "sort": "stars", "order": "desc", "per_page": 5},
        headers=github_headers(settings.github_token),
    )
    resp.raise_for_status()
    data = resp.json()
    n = int(data.get("total_count", 0))
    top = data.get("items", [])[:3]
    stars = max((r.get("stargazers_count", 0) for r in top), default=0)
    return round(math.log1p(n), 3), {
        "summary": f"近 90 天新建 {n} 个相关仓库，最高 ★{stars}",
        "count": n,
        "top": [{"name": r["full_name"], "stars": r.get("stargazers_count", 0)} for r in top],
    }


def validate_appstore(query: str, country: str | None, client: httpx.Client, **_: Any) -> Result:
    resp = client.get(
        "https://itunes.apple.com/search",
        params={
            "term": query,
            "country": (country or "us").lower(),
            "entity": "software",
            "limit": 50,
        },
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    n = len(results)
    ratings = sorted((r.get("userRatingCount") or 0 for r in results), reverse=True)
    top = results[:3]
    return round(n / 50, 3), {
        "summary": f"App Store 同类 {n} 个，头部评分数 {ratings[0] if ratings else 0}",
        "count": n,
        "top": [
            {
                "name": r.get("trackName"),
                "ratings": r.get("userRatingCount"),
                "rating": r.get("averageUserRating"),
            }
            for r in top
        ],
    }


_HIRING_CACHE: dict[str, Any] = {"ts": 0.0, "ids": []}


def _hiring_threads(client: httpx.Client, months: int = 6) -> list[int]:
    """最近几期 HN “Ask HN: Who is hiring?” 帖子 id（每月一期，缓存 12 小时）。"""
    if time.time() - _HIRING_CACHE["ts"] < 12 * 3600 and _HIRING_CACHE["ids"]:
        return _HIRING_CACHE["ids"]
    resp = client.get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "story,author_whoishiring", "hitsPerPage": 30},
    )
    resp.raise_for_status()
    ids = [
        int(h["objectID"])
        for h in resp.json().get("hits", [])
        if "who is hiring" in (h.get("title") or "").lower()
    ][:months]
    _HIRING_CACHE.update(ts=time.time(), ids=ids)
    return ids


def validate_jobs(query: str, client: httpx.Client, **_: Any) -> Result:
    """招聘需求：近半年 HN “Who is hiring” 帖中提到该词的招聘数（创业公司用人信号）。"""
    ids = _hiring_threads(client)
    if not ids:
        return None, {"summary": "未找到招聘帖"}
    tags = "comment,(" + ",".join(f"story_{i}" for i in ids) + ")"
    resp = client.get(
        "https://hn.algolia.com/api/v1/search",
        params={"query": query, "tags": tags, "hitsPerPage": 5},
    )
    resp.raise_for_status()
    data = resp.json()
    n = int(data.get("nbHits", 0))
    from groundswell.collectors.base import strip_html

    companies = [
        (strip_html(h.get("comment_text")) or "").split("|")[0].strip()[:60]
        for h in data.get("hits", [])
        if h.get("comment_text")
    ]
    return round(math.log1p(n), 3), {
        "summary": f"近 {len(ids)} 期 HN 招聘帖中 {n} 条提及",
        "count": n,
        "examples": [c for c in companies if c][:5],
    }


def validate_funding(query: str, client: httpx.Client, **_: Any) -> Result:
    since = int((now_utc() - timedelta(days=180)).timestamp())
    hits = 0
    titles: list[str] = []
    for word in ("raises", "funding", "seed round"):
        resp = client.get(
            "https://hn.algolia.com/api/v1/search",
            params={
                "query": f"{query} {word}",
                "tags": "story",
                "numericFilters": f"created_at_i>{since}",
                "hitsPerPage": 5,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        hits += int(data.get("nbHits", 0))
        titles += [h.get("title") for h in data.get("hits", []) if h.get("title")]
    return round(math.log1p(hits), 3), {
        "summary": f"近半年融资相关报道 {hits} 篇",
        "count": hits,
        "examples": list(dict.fromkeys(titles))[:5],
    }


POLICY_DAYS = 365


def validate_policy(query: str, **_: Any) -> Result:
    """已采集的政策文件（sources.yaml 中 category_hint=policy 的源）里近一年提及该话题的数量。"""
    from groundswell.configs import load_sources
    from groundswell.processing.terms import matches_any

    source_ids = [s.id for s in load_sources().sources if s.category_hint == "policy"]
    if not source_ids:
        return None, {"skipped": "未配置政策类数据源"}
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT title, url, coalesce(text, '') FROM raw_items
                WHERE source_id = ANY(:src) AND fetched_at >= now() - make_interval(days => :d)
                  AND (title ILIKE :q OR text ILIKE :q)
                ORDER BY fetched_at DESC LIMIT 200
                """
            ),
            {"src": source_ids, "d": POLICY_DAYS, "q": f"%{query}%"},
        ).all()
    hits = [(title, url) for title, url, body in rows if matches_any(f"{title} {body}", [query])]
    return round(math.log1p(len(hits)), 3), {
        "summary": f"近一年相关政策文件 {len(hits)} 份" if hits else "近一年无相关政策文件",
        "count": len(hits),
        "examples": [{"title": t, "url": u} for t, u in hits[:5]],
    }


VALIDATORS: dict[str, tuple[Callable[..., Result], bool]] = {
    # kind: (函数, 是否需要英文查询)
    "gtrends_5y": (validate_gtrends, False),
    "github": (validate_github, True),
    "appstore": (validate_appstore, False),
    "jobs": (validate_jobs, True),
    "funding": (validate_funding, True),
    "policy": (validate_policy, False),
}


def _targets(days: int, limit: int) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            text(
                """
                SELECT DISTINCT ON (o.topic_id, o.region) o.topic_id, o.region, t.name, t.keywords,
                       coalesce(o.rank_score, o.total_score) AS score
                FROM opportunities o JOIN topics t ON t.id = o.topic_id
                WHERE o.stage = 'deep' AND o.created_at >= now() - make_interval(days => :d)
                ORDER BY o.topic_id, o.region, o.created_at DESC
                """
            ),
            {"d": days},
        ).all()
    rows = sorted((dict(r._mapping) for r in rows), key=lambda r: -(r["score"] or 0))
    return rows[:limit]


def _recent_kinds(topic_id: int, region: str) -> set[str]:
    with session_scope() as s:
        return set(
            s.execute(
                text(
                    """
                    SELECT kind FROM validations WHERE topic_id = :t AND region = :r
                      AND ts >= now() - make_interval(days => :d)
                    """
                ),
                {"t": topic_id, "r": region, "d": CACHE_DAYS},
            ).scalars()
        )


def validate_one(
    topic_id: int,
    region: str,
    name: str,
    keywords: list[str],
    *,
    regions: RegionsFile,
    settings: Settings,
    client: httpx.Client,
    kinds: tuple[str, ...] | None = None,
) -> dict[str, str]:
    done = _recent_kinds(topic_id, region)
    local, en = query_terms(name, keywords)
    country = regions.primary_country(region) if region != GLOBAL else None
    out: dict[str, str] = {}
    for kind, (fn, needs_en) in VALIDATORS.items():
        if kinds and kind not in kinds:
            continue
        if kind in done:
            out[kind] = "cached"
            continue
        query = en if needs_en else local
        if not query:
            out[kind] = "skipped"
            continue
        try:
            signal, payload = fn(query=query, country=country, client=client, settings=settings)
        except Exception as exc:
            logger.warning(f"验证器 {kind} 失败（{query}）：{exc}")
            out[kind] = "error"
            continue
        with session_scope() as s:
            s.add(
                Validation(
                    topic_id=topic_id,
                    region=region,
                    kind=kind,
                    query=query,
                    signal=signal,
                    payload=payload,
                )
            )
        out[kind] = "ok"
        time.sleep(0.5)
    return out


def run_validations(
    days: int = 1, limit: int = 30, kinds: tuple[str, ...] | None = None
) -> dict[str, Any]:
    settings = get_settings()
    regions = load_regions()
    targets = _targets(days, limit)
    summary: dict[str, int] = {}
    with _client(settings) as client:
        for t in targets:
            res = validate_one(
                t["topic_id"],
                t["region"],
                t["name"],
                list(t["keywords"] or []),
                regions=regions,
                settings=settings,
                client=client,
                kinds=kinds,
            )
            for status in res.values():
                summary[status] = summary.get(status, 0) + 1
    return {"targets": len(targets), **summary}
