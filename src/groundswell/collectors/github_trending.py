"""GitHub 新晋热门仓库（Search API 近似 Trending，避免抓页面）。

star 增速由 item_snapshots 的历史 stars 差分得到。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from groundswell.collectors.base import Collector, Item, parse_iso
from groundswell.collectors.registry import register

API = "https://api.github.com/search/repositories"


def github_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@register("github_trending")
class GitHubTrendingCollector(Collector):
    """params:
    created_within_days: 只看最近 N 天创建的仓库
    min_stars: star 下限
    queries: 额外的检索条件列表（如 "topic:llm"），与基础条件 AND
    """

    def fetch(self) -> list[Item]:
        days = int(self.params.get("created_within_days", 7))
        min_stars = int(self.params.get("min_stars", 50))
        per_page = int(self.params.get("per_page", 50))
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        base_q = f"created:>{since} stars:>={min_stars}"
        queries = [None, *self.params.get("queries", [])]
        headers = github_headers(self.settings.github_token)

        items: dict[str, Item] = {}
        for extra in queries:
            q = f"{base_q} {extra}" if extra else base_q
            data = self.get_json(
                API,
                params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page},
                headers=headers,
            )
            for rank, repo in enumerate(data.get("items", []), start=1):
                name = repo["full_name"]
                if name in items:
                    continue
                desc = repo.get("description") or ""
                topics = repo.get("topics") or []
                items[name] = Item(
                    external_id=name,
                    title=f"{name}: {desc}"[:500] if desc else name,
                    url=repo.get("html_url"),
                    text=" ".join([desc, *topics]).strip() or None,
                    author=(repo.get("owner") or {}).get("login"),
                    published_at=parse_iso(repo.get("created_at")),
                    metrics={
                        "stars": repo.get("stargazers_count") or 0,
                        "forks": repo.get("forks_count") or 0,
                        "language": repo.get("language"),
                        "topics": topics,
                    },
                    channel=extra or "new_repos",
                    rank=rank if extra is None else None,
                )
        return list(items.values())
