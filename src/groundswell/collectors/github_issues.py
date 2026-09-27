"""竞品监控：竞品仓库里点赞最多、仍未关闭的 issue（= 用户公开投票但未实现的需求）。"""

from __future__ import annotations

import re

from groundswell.collectors.base import Collector, CollectorError, Item, parse_iso, strip_html
from groundswell.collectors.github_trending import github_headers
from groundswell.collectors.registry import register

API = "https://api.github.com/search/issues"

# issue 模板里的标题、勾选框、注释是所有 issue 共有的噪声
_TEMPLATE_LINE = re.compile(r"^\s*(#{1,6}\s|[-*]\s*\[[ xX]\]|<!--|-->|>\s*\[!|\|)")


def clean_issue_body(body: str | None, limit: int = 1000) -> str | None:
    if not body:
        return None
    lines = [ln for ln in body.splitlines() if ln.strip() and not _TEMPLATE_LINE.match(ln)]
    return strip_html("\n".join(lines), limit=limit) or None


@register("github_issues")
class GitHubIssuesCollector(Collector):
    """params:
    repos: ["owner/name", ...]
    per_repo: 每个仓库取前 N 条
    """

    def fetch(self) -> list[Item]:
        repos = self.params.get("repos", [])
        per_repo = int(self.params.get("per_repo", 20))
        headers = github_headers(self.settings.github_token)
        items: list[Item] = []
        errors: list[str] = []
        for repo in repos:
            try:
                data = self.get_json(
                    API,
                    params={
                        "q": f"repo:{repo} is:issue is:open",
                        "sort": "reactions-+1",
                        "order": "desc",
                        "per_page": per_repo,
                    },
                    headers=headers,
                )
            except CollectorError as exc:
                errors.append(f"{repo}: {exc}")
                continue
            for rank, issue in enumerate(data.get("items", []), start=1):
                reactions = issue.get("reactions") or {}
                items.append(
                    Item(
                        external_id=issue["html_url"],
                        # 仓库名放在 channel，不进标题，避免同仓库无关 issue 因前缀相同被聚到一起
                        title=str(issue["title"])[:500],
                        url=issue["html_url"],
                        text=clean_issue_body(issue.get("body")),
                        author=(issue.get("user") or {}).get("login"),
                        published_at=parse_iso(issue.get("created_at")),
                        metrics={
                            "reactions": reactions.get("+1", 0),
                            "comments": issue.get("comments") or 0,
                        },
                        channel=repo,
                        rank=rank,
                    )
                )
        if errors and not items:
            raise CollectorError("; ".join(errors[:5]))
        return items
