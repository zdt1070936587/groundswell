from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text

from groundswell.config import Settings

TEST_DB_URL = os.getenv("GS_TEST_DATABASE_URL")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None, database_url=TEST_DB_URL or "postgresql+psycopg://x@127.0.0.1:1/x"
    )


@pytest.fixture
def mock_client() -> Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]:
    def make(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler))

    return make


@pytest.fixture(scope="session")
def _migrated_db() -> Iterator[str]:
    if not TEST_DB_URL:
        pytest.skip("未设置 GS_TEST_DATABASE_URL，跳过数据库测试")
    from groundswell.db import migrate
    from groundswell.db.session import reset_engine

    migrate.upgrade(url=TEST_DB_URL)
    reset_engine(TEST_DB_URL)
    yield TEST_DB_URL


@pytest.fixture
def db(_migrated_db: str) -> Iterator[None]:
    """每个用例前清空业务表。"""
    from groundswell.db.models import Base
    from groundswell.db.session import get_engine

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield
