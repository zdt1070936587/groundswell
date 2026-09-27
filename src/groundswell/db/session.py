from __future__ import annotations

import zlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from groundswell.config import get_settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def reset_engine(url: str | None = None) -> Engine:
    """测试或 CLI 切换数据库时使用。"""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url or get_settings().database_url, pool_pre_ping=True)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _lock_key(name: str) -> int:
    return zlib.crc32(f"groundswell:{name}".encode()) & 0x7FFFFFFF


@contextmanager
def advisory_lock(name: str) -> Iterator[bool]:
    """跨进程互斥：同名阶段同一时刻只有一个实例在跑（调度器、worker、手动 CLI 共用）。

    锁挂在独立连接上，不受业务 session 提交的影响。
    """
    key = _lock_key(name)
    with get_engine().connect() as conn:
        acquired = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
