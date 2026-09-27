"""RSS / Atom 源。

- kind=rsshub：params.route 为 RSSHub 路由（如 /weibo/search/hot），地址取自 GS_RSSHUB_URL
- kind=rss：params.url 为任意 RSS 地址（区域科技媒体、政策发布、竞品 changelog）
"""

from __future__ import annotations

import hashlib

import feedparser

from groundswell.collectors.base import (
    Collector,
    CollectorError,
    Item,
    from_struct_time,
    parse_int,
    strip_html,
)
from groundswell.collectors.registry import register


def parse_feed(content: bytes | str, *, channel: str | None = None, limit: int = 100) -> list[Item]:
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise CollectorError(f"RSS 解析失败: {feed.bozo_exception}")
    items: list[Item] = []
    for rank, entry in enumerate(feed.entries[:limit], start=1):
        title = strip_html(entry.get("title"), limit=500)
        if not title:
            continue
        link = entry.get("link")
        ext = entry.get("id") or link or hashlib.sha1(title.encode()).hexdigest()
        summary = entry.get("summary") or ""
        if entry.get("content"):
            summary = entry["content"][0].get("value") or summary
        metrics: dict = {}
        # RSSHub 部分热榜会在描述里带热度值，保留排名作为通用信号
        heat = parse_int(entry.get("heat") or entry.get("hot"))
        if heat:
            metrics["heat"] = heat
        items.append(
            Item(
                external_id=str(ext)[:512],
                title=title,
                url=link,
                text=strip_html(summary),
                author=entry.get("author"),
                published_at=from_struct_time(
                    entry.get("published_parsed") or entry.get("updated_parsed")
                ),
                metrics=metrics,
                channel=channel,
                rank=rank,
            )
        )
    return items


@register("rsshub")
class RSSHubCollector(Collector):
    def fetch(self) -> list[Item]:
        route = self.params["route"]
        base = self.settings.rsshub_url.rstrip("/")
        resp = self.request("GET", f"{base}{route}")
        return parse_feed(resp.content, channel=route, limit=int(self.params.get("limit", 100)))


@register("rss")
class RSSCollector(Collector):
    def fetch(self) -> list[Item]:
        urls = self.params.get("urls") or [self.params["url"]]
        items: list[Item] = []
        for url in urls:
            resp = self.request("GET", url)
            items.extend(
                parse_feed(
                    resp.content,
                    channel=self.params.get("channel"),
                    limit=int(self.params.get("limit", 50)),
                )
            )
        return items
