"""Reddit。

配置了 GS_REDDIT_CLIENT_ID/SECRET 时走官方 OAuth API（application-only），
否则退化为公开 JSON（限流严格，仅用于开发）。channels 为 subreddit → 国家/区域映射。
"""

from __future__ import annotations

import time

from loguru import logger

from groundswell.collectors.base import Collector, CollectorError, Item, from_epoch, strip_html
from groundswell.collectors.registry import register

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"


@register("reddit")
class RedditCollector(Collector):
    _token: tuple[str, float] | None = None

    def _auth_headers(self) -> dict[str, str]:
        s = self.settings
        if not (s.reddit_client_id and s.reddit_client_secret):
            return {}
        cached = RedditCollector._token
        if cached and cached[1] > time.time() + 60:
            return {"Authorization": f"Bearer {cached[0]}"}
        resp = self.request(
            "POST",
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(s.reddit_client_id, s.reddit_client_secret),
        )
        data = resp.json()
        RedditCollector._token = (
            data["access_token"],
            time.time() + float(data.get("expires_in", 3600)),
        )
        return {"Authorization": f"Bearer {data['access_token']}"}

    def fetch(self) -> list[Item]:
        listing = self.params.get("listing", "hot")
        limit = int(self.params.get("limit", 50))
        headers = self._auth_headers()
        base = "https://oauth.reddit.com" if headers else "https://www.reddit.com"
        suffix = "" if headers else ".json"
        if not headers:
            logger.info(f"{self.source.id}: 未配置 Reddit OAuth，使用公开 JSON")

        items: list[Item] = []
        errors: list[str] = []
        for sub in self.source.channels or {
            name: None for name in self.params.get("subreddits", [])
        }:
            try:
                data = self.get_json(
                    f"{base}/r/{sub}/{listing}{suffix}",
                    params={"limit": limit, "raw_json": 1},
                    headers=headers,
                )
            except CollectorError as exc:
                errors.append(f"r/{sub}: {exc}")
                continue
            for rank, child in enumerate(data.get("data", {}).get("children", []), start=1):
                post = child.get("data", {})
                if post.get("stickied") or not post.get("title"):
                    continue
                items.append(
                    Item(
                        external_id=post["name"],
                        title=post["title"],
                        url=f"https://www.reddit.com{post.get('permalink', '')}",
                        text=strip_html(post.get("selftext"), limit=1000),
                        author=post.get("author"),
                        published_at=from_epoch(post.get("created_utc")),
                        metrics={
                            "score": post.get("score") or 0,
                            "comments": post.get("num_comments") or 0,
                            "upvote_ratio": post.get("upvote_ratio"),
                            "link": post.get("url"),
                        },
                        channel=sub,
                        rank=rank,
                    )
                )
            time.sleep(float(self.params.get("delay_seconds", 1.0)))
        if errors and not items:
            raise CollectorError("; ".join(errors[:5]))
        if errors:
            logger.warning(f"{self.source.id}: 部分 subreddit 失败 {errors[:5]}")
        return items
