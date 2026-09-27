"""Google Trends 每日趋势（按国家 geo 逐个拉取）。

主路径：官方 RSS（https://trends.google.com/trending/rss?geo=XX），稳定且不需要代理池；
降级：pytrends.trending_searches（非官方接口，易 429，需安装 trends 可选依赖）。
"""

from __future__ import annotations

import time

import feedparser
from loguru import logger

from groundswell.collectors.base import Collector, CollectorError, Item, from_struct_time, parse_int
from groundswell.collectors.registry import register

RSS_URL = "https://trends.google.com/trending/rss"

# pytrends.trending_searches 使用的国家名
PYTRENDS_PN = {
    "US": "united_states",
    "CA": "canada",
    "GB": "united_kingdom",
    "DE": "germany",
    "FR": "france",
    "IT": "italy",
    "ES": "spain",
    "NL": "netherlands",
    "SE": "sweden",
    "PL": "poland",
    "BR": "brazil",
    "MX": "mexico",
    "AR": "argentina",
    "CO": "colombia",
    "CL": "chile",
    "SG": "singapore",
    "MY": "malaysia",
    "ID": "indonesia",
    "TH": "thailand",
    "VN": "vietnam",
    "PH": "philippines",
    "HK": "hong_kong",
    "TW": "taiwan",
}


def parse_trends_rss(content: bytes | str, geo: str) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise CollectorError(f"Trends RSS 解析失败: {feed.bozo_exception}")
    items: list[Item] = []
    for rank, entry in enumerate(feed.entries, start=1):
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        published = from_struct_time(entry.get("published_parsed"))
        day = published.strftime("%Y-%m-%d") if published else "na"
        news_title = entry.get("ht_news_item_title")
        news_url = entry.get("ht_news_item_url")
        items.append(
            Item(
                external_id=f"{geo}:{title.lower()}:{day}",
                title=title,
                url=news_url or f"https://trends.google.com/trends/explore?geo={geo}&q={title}",
                text=news_title,
                published_at=published,
                metrics={"traffic": parse_int(entry.get("ht_approx_traffic")) or 0},
                channel=geo,
                country=geo,
                rank=rank,
            )
        )
    return items


@register("google_trends")
class GoogleTrendsCollector(Collector):
    def _pytrends(self, geo: str) -> list[Item]:
        try:
            from pytrends.request import TrendReq
        except ImportError as exc:
            raise CollectorError("RSS 失败且未安装 pytrends（uv sync --extra trends）") from exc
        pn = PYTRENDS_PN.get(geo)
        if not pn:
            raise CollectorError(f"pytrends 不支持 geo={geo}")
        df = TrendReq(hl="en-US", tz=0).trending_searches(pn=pn)
        return [
            Item(
                external_id=f"{geo}:{str(v).lower()}:pytrends",
                title=str(v),
                channel=geo,
                country=geo,
                rank=i,
            )
            for i, v in enumerate(df[0].tolist(), start=1)
        ]

    def fetch(self) -> list[Item]:
        geos = list(self.source.channels) or self.params.get("geos", ["US"])
        items: list[Item] = []
        errors: list[str] = []
        for geo in geos:
            try:
                resp = self.request("GET", RSS_URL, params={"geo": geo})
                items.extend(parse_trends_rss(resp.content, geo))
            except CollectorError as exc:
                logger.warning(f"{self.source.id}: RSS geo={geo} 失败，尝试 pytrends：{exc}")
                try:
                    items.extend(self._pytrends(geo))
                except Exception as exc2:  # noqa: BLE001
                    errors.append(f"{geo}: {exc2}")
            time.sleep(float(self.params.get("delay_seconds", 0.5)))
        if errors and not items:
            raise CollectorError("; ".join(errors[:5]))
        return items
