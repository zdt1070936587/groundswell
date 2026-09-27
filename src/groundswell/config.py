from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parents[1]


def _default_config_dir() -> Path:
    candidate = REPO_ROOT / "configs"
    return candidate if candidate.exists() else Path.cwd() / "configs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GS_",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://groundswell:groundswell@127.0.0.1:5432/groundswell"
    config_dir: Path = Field(default_factory=_default_config_dir)
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")

    http_timeout: float = 20.0
    http_proxy: str | None = None
    user_agent: str = "Mozilla/5.0 (compatible; groundswell/0.1; +https://github.com/groundswell)"

    rsshub_url: str = "http://127.0.0.1:1200"
    dailyhot_url: str = "http://127.0.0.1:6688"
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    github_token: str | None = None

    dingtalk_webhook: str | None = None
    dingtalk_secret: str | None = None

    embedding_provider: Literal["hash", "api", "local"] = "hash"
    embedding_api_base: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str | None = None
    embedding_model: str = "BAAI/bge-m3"
    cluster_threshold: float | None = None
    merge_threshold: float | None = None

    llm_cheap_model: str | None = None
    llm_strong_model: str | None = None
    llm_daily_budget_cny: float = 20.0
    usd_to_cny: float = 7.2
    llm_deep_daily_cap: int = 50

    trend_window_hours: int = 48
    trend_min_count: float = 3.0
    trend_z_threshold: float = 2.0

    report_timezone: str = "Asia/Shanghai"
    report_hour: int = 8
    report_regions: list[str] = ["GLOBAL", "GC", "NA", "SEA", "LATAM", "EU"]

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: str | None = None

    redis_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
