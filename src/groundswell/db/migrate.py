from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from groundswell.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().database_url).replace("%", "%%"))
    return cfg


def upgrade(revision: str = "head", url: str | None = None) -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(revision: str, url: str | None = None) -> None:
    command.downgrade(alembic_config(url), revision)


def current(url: str | None = None) -> None:
    command.current(alembic_config(url), verbose=False)


def check(url: str | None = None) -> None:
    """模型与数据库结构不一致时抛出异常。"""
    command.check(alembic_config(url))
