"""采集器协议。

新增数据源 = 新增一个 Collector 子类并用 @register 注册，再在 configs/sources.yaml 里声明实例。
采集器只负责把外部数据转换成 Item，入库、去重、区域归属都由下游统一处理。
"""

from __future__ import annotations

import html
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar

import httpx
from loguru import logger

from groundswell.config import Settings
from groundswell.configs import SourceConfig

MAX_TEXT = 2000


@dataclass
class Item:
    external_id: str
    title: str
    url: str | None = None
    text: str | None = None
    author: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    channel: str | None = None
    country: str | None = None
    language: str | None = None
    rank: int | None = None


class CollectorError(RuntimeError):
    pass


class Collector(ABC):
    kind: ClassVar[str]

    def __init__(self, source: SourceConfig, settings: Settings, client: httpx.Client):
        self.source = source
        self.settings = settings
        self.client = client

    @property
    def params(self) -> dict[str, Any]:
        return self.source.params

    @abstractmethod
    def fetch(self) -> list[Item]:
        """返回本次抓取到的条目。失败时抛异常，由 runner 记录到源健康表。"""

    # ------------------------------------------------------------ HTTP 辅助

    def request(
        self,
        method: str,
        url: str,
        *,
        retries: int = 3,
        backoff: float = 2.0,
        **kwargs: Any,
    ) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self.client.request(method, url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = float(resp.headers.get("Retry-After") or backoff * (2**attempt))
                    logger.warning(f"{self.source.id}: HTTP {resp.status_code}，{wait:.0f}s 后重试")
                    last_exc = CollectorError(f"HTTP {resp.status_code} {url}")
                    time.sleep(min(wait, 30))
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as exc:
                raise CollectorError(f"HTTP {exc.response.status_code} {url}") from exc
            except httpx.TransportError as exc:
                last_exc = exc
                time.sleep(backoff * (2**attempt))
        raise CollectorError(f"请求失败 {url}: {last_exc}") from last_exc

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs).json()

    def get_text(self, url: str, **kwargs: Any) -> str:
        return self.request("GET", url, **kwargs).text


# ---------------------------------------------------------------- 工具函数

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: str | None, limit: int = MAX_TEXT) -> str | None:
    if not value:
        return None
    text = html.unescape(_TAG_RE.sub(" ", value))
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit] or None


def from_epoch(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def from_struct_time(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime(*value[:6], tzinfo=UTC)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_int(value: Any) -> int | None:
    """'200,000+' / '2K+' / 1234 → int。"""
    if value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    s = str(value).strip().replace(",", "").rstrip("+").upper()
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None
