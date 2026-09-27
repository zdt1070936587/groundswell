from __future__ import annotations

import pgvector.sqlalchemy  # noqa: F401  注册 vector 类型用于反射比对
from alembic import context
from sqlalchemy import engine_from_config, pool

from groundswell.config import get_settings
from groundswell.db.models import Base

config = context.config
target_metadata = Base.metadata

# Timescale 连续聚合、扩展自带的对象不参与比对
IGNORED_TABLES = {"topic_daily_stats"}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and (name in IGNORED_TABLES or (reflected and compare_to is None)))


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
