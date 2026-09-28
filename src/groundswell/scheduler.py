"""常驻服务：APScheduler（后台线程）+ FastAPI（uvicorn 主线程）。

任务之间用 PostgreSQL advisory lock 互斥，因此即使同时启动了 `gs serve` 和 `gs worker`，
同一阶段也不会并发执行。配置了 GS_REDIS_URL 时，定时任务只负责投递到队列，由 worker 执行；
到期采集按源拆成 `collect:<source_id>` 任务，多个 worker 并行抓取。
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger

from groundswell.config import get_settings

JOB_NAMES = ("collect_due", "hourly", "daily", "nightly", "weekly", "monthly")


def _job_fn(name: str) -> Callable[[], Any]:
    from groundswell import pipeline

    return {
        "collect_due": pipeline.run_collect_due,
        "hourly": lambda: pipeline.run_hourly(collect_first=False),
        "daily": lambda: pipeline.run_daily(push=True),
        "nightly": pipeline.run_nightly,
        "weekly": lambda: pipeline.run_weekly(push=True),
        "monthly": lambda: pipeline.run_monthly(push=True),
    }[name]


def run_job(name: str) -> Any:
    """执行（或投递）一个任务；异常只记录不抛出，避免杀死调度线程。"""
    settings = get_settings()
    if settings.redis_url:
        from groundswell.taskqueue import enqueue, enqueue_due_collections

        try:
            if name == "collect_due":
                return f"enqueued {enqueue_due_collections()} sources"
            return "enqueued" if enqueue(name) else "already_queued"
        except Exception:
            logger.exception(f"任务 {name} 投递失败")
            return None
    try:
        result = _job_fn(name)()
        logger.info(f"任务 {name} 完成: {str(result)[:500]}")
        return result
    except Exception:
        logger.exception(f"任务 {name} 失败")
        from groundswell.ops.health import record_alert

        record_alert("job_failed", name, f"定时任务 {name} 执行失败，详见日志")
        return None


def build_scheduler() -> BackgroundScheduler:
    settings = get_settings()
    tz = settings.report_timezone
    sched = BackgroundScheduler(
        timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 1800}
    )
    sched.add_job(run_job, IntervalTrigger(minutes=15), args=["collect_due"], id="collect_due")
    sched.add_job(run_job, CronTrigger(minute=7), args=["hourly"], id="hourly")
    sched.add_job(
        run_job, CronTrigger(hour=settings.report_hour, minute=0), args=["daily"], id="daily"
    )
    sched.add_job(run_job, CronTrigger(hour=3, minute=0), args=["nightly"], id="nightly")
    sched.add_job(
        run_job,
        CronTrigger(day_of_week="mon", hour=settings.report_hour, minute=30),
        args=["weekly"],
        id="weekly",
    )
    sched.add_job(
        run_job,
        CronTrigger(day=1, hour=settings.report_hour, minute=45),
        args=["monthly"],
        id="monthly",
    )
    return sched


def serve(api: bool = True, scheduler: bool = True) -> None:
    settings = get_settings()
    sched = None
    if scheduler:
        sched = build_scheduler()
        sched.start()
        for job in sched.get_jobs():
            logger.info(f"调度 {job.id}: 下次 {job.next_run_time}")
        # 启动时先跑一次到期采集，避免重启后空等 15 分钟
        threading.Thread(target=run_job, args=("collect_due",), daemon=True).start()
    try:
        if api:
            import uvicorn

            from groundswell.api.app import create_app

            uvicorn.run(
                create_app(), host=settings.api_host, port=settings.api_port, log_level="info"
            )
        else:
            stop = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            stop.wait()
    finally:
        if sched:
            sched.shutdown(wait=False)
