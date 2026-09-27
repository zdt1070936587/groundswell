"""DailyHotApi（自建，imsyy/dailyhot-api）：补 RSSHub 未覆盖的国内热榜，如抖音、头条、快手。"""

from __future__ import annotations

from groundswell.collectors.base import (
    Collector,
    CollectorError,
    Item,
    from_epoch,
    parse_int,
    strip_html,
)
from groundswell.collectors.registry import register


def parse_dailyhot(data: dict, route: str) -> list[Item]:
    if not isinstance(data, dict) or data.get("code") != 200:
        raise CollectorError(f"DailyHotApi {route} 返回异常: {str(data)[:200]}")
    items: list[Item] = []
    for rank, row in enumerate(data.get("data") or [], start=1):
        title = (row.get("title") or "").strip()
        if not title:
            continue
        ts = row.get("timestamp")
        if isinstance(ts, int | float) and ts > 1e12:
            ts = ts / 1000
        items.append(
            Item(
                external_id=str(row.get("id") or row.get("url") or title)[:512],
                title=title,
                url=row.get("url") or row.get("mobileUrl"),
                text=strip_html(row.get("desc")),
                author=row.get("author"),
                published_at=from_epoch(ts) if isinstance(ts, int | float) else None,
                metrics={"heat": parse_int(row.get("hot")) or 0},
                channel=route,
                rank=rank,
            )
        )
    return items


@register("dailyhot")
class DailyHotCollector(Collector):
    def fetch(self) -> list[Item]:
        route = self.params["route"]
        base = self.settings.dailyhot_url.rstrip("/")
        data = self.get_json(
            f"{base}/{route}", params={"cache": "false"} if self.params.get("no_cache") else None
        )
        return parse_dailyhot(data, route)[: int(self.params.get("limit", 50))]
