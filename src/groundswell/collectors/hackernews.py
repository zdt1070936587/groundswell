"""Hacker News（Algolia 公开 API，无需鉴权）。"""

from __future__ import annotations

import time

from groundswell.collectors.base import Collector, Item, from_epoch, strip_html
from groundswell.collectors.registry import register

API = "https://hn.algolia.com/api/v1"


@register("hackernews")
class HackerNewsCollector(Collector):
    """params:
    tags: 频道列表，front_page / show_hn / ask_hn / story
    hits_per_page: 每个频道条数
    lookback_hours / min_points: 非 front_page 频道只取近期且达到分数线的帖子
    """

    def fetch(self) -> list[Item]:
        tags = self.params.get("tags", ["front_page", "show_hn", "ask_hn"])
        per_page = int(self.params.get("hits_per_page", 50))
        since = int(time.time()) - int(self.params.get("lookback_hours", 24)) * 3600
        min_points = int(self.params.get("min_points", 10))

        items: list[Item] = []
        for tag in tags:
            if tag == "front_page":
                data = self.get_json(f"{API}/search", params={"tags": tag, "hitsPerPage": per_page})
            else:
                data = self.get_json(
                    f"{API}/search_by_date",
                    params={
                        "tags": tag,
                        "hitsPerPage": per_page,
                        "numericFilters": f"created_at_i>{since},points>={min_points}",
                    },
                )
            for rank, hit in enumerate(data.get("hits", []), start=1):
                title = hit.get("title") or hit.get("story_title")
                if not title:
                    continue
                oid = hit["objectID"]
                items.append(
                    Item(
                        external_id=oid,
                        title=title,
                        url=hit.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                        text=strip_html(hit.get("story_text")),
                        author=hit.get("author"),
                        published_at=from_epoch(hit.get("created_at_i")),
                        metrics={
                            "points": hit.get("points") or 0,
                            "comments": hit.get("num_comments") or 0,
                            "hn_url": f"https://news.ycombinator.com/item?id={oid}",
                        },
                        channel=tag,
                        rank=rank if tag == "front_page" else None,
                    )
                )
        return items
