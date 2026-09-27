"""App Store 各国免费/付费榜（Apple Marketing Tools 公开 RSS JSON）。

低成本的“通用区域源”：每个国家一个榜单，榜单新面孔与排名跃升是消费需求的先行信号。
"""

from __future__ import annotations

from loguru import logger

from groundswell.collectors.base import Collector, CollectorError, Item, parse_iso
from groundswell.collectors.registry import register

URL = "https://rss.marketingtools.apple.com/api/v2/{cc}/apps/{chart}/{limit}/apps.json"


@register("appstore")
class AppStoreChartsCollector(Collector):
    """channels: 国家代码（小写/大写均可）→ 国家映射；params.chart: top-free / top-paid"""

    def fetch(self) -> list[Item]:
        chart = self.params.get("chart", "top-free")
        limit = int(self.params.get("limit", 50))
        items: list[Item] = []
        errors: list[str] = []
        for cc in self.source.channels:
            try:
                data = self.get_json(URL.format(cc=cc.lower(), chart=chart, limit=limit))
            except CollectorError as exc:
                errors.append(f"{cc}: {exc}")
                continue
            for rank, app in enumerate(data.get("feed", {}).get("results", []), start=1):
                genres = [g.get("name") for g in app.get("genres", []) if g.get("name")]
                items.append(
                    Item(
                        external_id=f"{cc.upper()}:{chart}:{app['id']}",
                        title=str(app.get("name") or app["id"]),
                        url=app.get("url"),
                        published_at=parse_iso(app.get("releaseDate")),
                        metrics={
                            "app_id": app["id"],
                            "chart": chart,
                            "genres": genres,
                            "developer": app.get("artistName"),
                        },
                        channel=cc,
                        country=cc.upper(),
                        rank=rank,
                    )
                )
        if errors and not items:
            raise CollectorError("; ".join(errors[:5]))
        if errors:
            logger.warning(f"{self.source.id}: 部分国家失败 {errors[:5]}")
        return items
