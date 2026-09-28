"""Redis 任务队列（GS_REDIS_URL）：调度器只投递，`gs worker` 执行，worker 可水平扩展。

- 任务名：scheduler.JOB_NAMES 中的流水线任务，或 `collect:<source_id>`（单源采集，多 worker 并行抓取）；
- 去重：任务在队列中或执行中时不会被重复投递（集合 gs:queued，执行结束后移除）；
- 同名流水线阶段仍由 PostgreSQL advisory lock 互斥，多 worker 不会并发跑同一阶段。
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from typing import Any

from loguru import logger

from groundswell.config import get_settings
from groundswell.processing.timeutil import now_utc

QUEUE_KEY = "gs:jobs"
QUEUED_KEY = "gs:queued"


def get_client(url: str | None = None) -> Any:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("需要 redis 包：uv sync --extra queue") from exc
    url = url or get_settings().redis_url
    if not url:
        raise RuntimeError("未配置 GS_REDIS_URL")
    return redis.Redis.from_url(url, decode_responses=True)


def enqueue(name: str, client: Any = None) -> bool:
    client = client or get_client()
    if not client.sadd(QUEUED_KEY, name):
        logger.info(f"任务 {name} 已在队列中，跳过")
        return False
    client.lpush(QUEUE_KEY, json.dumps({"name": name, "ts": now_utc().isoformat()}))
    return True


def enqueue_due_collections(client: Any = None) -> int:
    from groundswell.collectors.runner import due_sources, sync_sources
    from groundswell.configs import load_sources
    from groundswell.db.session import session_scope

    sources = load_sources()
    with session_scope() as s:
        sync_sources(s, sources)
        due = due_sources(s, sources)
    client = client or get_client()
    return sum(enqueue(f"collect:{cfg.id}", client) for cfg in due)


def resolve(name: str) -> Callable[[], Any]:
    if name.startswith("collect:"):
        from groundswell.collectors.runner import collect

        source_id = name.split(":", 1)[1]
        return lambda: [r.__dict__ for r in collect([source_id])]
    from groundswell.scheduler import _job_fn

    return _job_fn(name)


def run_one(client: Any, timeout: int = 5) -> str | None:
    """取出并执行一个任务；队列为空返回 None。"""
    popped = client.brpop(QUEUE_KEY, timeout=timeout)
    if not popped:
        return None
    _, raw = popped
    name = json.loads(raw)["name"]
    try:
        result = resolve(name)()
        logger.info(f"worker 任务 {name} 完成: {str(result)[:300]}")
    except Exception:
        logger.exception(f"worker 任务 {name} 失败")
        from groundswell.ops.health import record_alert

        record_alert("job_failed", name, f"队列任务 {name} 执行失败，详见 worker 日志")
    finally:
        client.srem(QUEUED_KEY, name)
    return name


def run_worker(client: Any = None, stop: threading.Event | None = None) -> None:
    client = client or get_client()
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logger.info("worker 已启动，等待任务")
    while not stop.is_set():
        run_one(client)
    logger.info("worker 已停止")
