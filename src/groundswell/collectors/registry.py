from __future__ import annotations

import importlib

import httpx

from groundswell.collectors.base import Collector
from groundswell.config import Settings
from groundswell.configs import SourceConfig

COLLECTORS: dict[str, type[Collector]] = {}

BUILTIN_MODULES = (
    "hackernews",
    "rss",
    "dailyhot",
    "reddit",
    "github_trending",
    "github_issues",
    "google_trends",
    "appstore",
)


def register(kind: str):
    def deco(cls: type[Collector]) -> type[Collector]:
        cls.kind = kind
        COLLECTORS[kind] = cls
        return cls

    return deco


def load_builtin_collectors() -> None:
    for mod in BUILTIN_MODULES:
        importlib.import_module(f"groundswell.collectors.{mod}")


def make_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=settings.http_timeout,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.http_proxy,
        follow_redirects=True,
    )


def build_collector(source: SourceConfig, settings: Settings, client: httpx.Client) -> Collector:
    load_builtin_collectors()
    try:
        cls = COLLECTORS[source.kind]
    except KeyError as exc:
        raise ValueError(f"未知采集器类型: {source.kind}") from exc
    return cls(source, settings, client)
