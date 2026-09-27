"""源健康检查与告警。告警按 (kind, key) 去重，冷却期内不重复推送。"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select, text

from groundswell import queries
from groundswell.config import get_settings
from groundswell.configs import load_sources
from groundswell.db.models import Alert
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import now_utc

COOLDOWN = timedelta(hours=6)
FAIL_THRESHOLD = 3


def record_alert(
    kind: str, key: str, message: str, *, push: bool = True, cooldown: timedelta = COOLDOWN
) -> bool:
    """写入告警；冷却期内同一 (kind, key) 只记一次。返回是否为新告警。"""
    with session_scope() as s:
        recent = s.execute(
            select(Alert.id).where(
                Alert.kind == kind, Alert.key == key, Alert.ts >= now_utc() - cooldown
            )
        ).first()
        if recent:
            return False
        s.add(Alert(kind=kind, key=key, message=message))
    logger.warning(f"[告警] {kind}/{key}: {message}")
    if push:
        from groundswell.reporting.dingtalk import send_markdown

        send_markdown("观潮告警", f"#### 观潮告警 · {kind}\n\n{message}")
    return True


def find_problems() -> list[tuple[str, str, str]]:
    """返回 (kind, key, message) 列表。"""
    settings = get_settings()
    sources = {s.id: s for s in load_sources().sources}
    problems: list[tuple[str, str, str]] = []
    now = now_utc()
    with session_scope() as s:
        for row in queries.source_health(s, hours=24):
            cfg = sources.get(row["id"])
            if not row["enabled"] or cfg is None or not cfg.enabled:
                continue
            if row["consecutive_failures"] >= FAIL_THRESHOLD:
                problems.append(
                    (
                        "source_failing",
                        row["id"],
                        f"数据源 `{row['id']}` 连续失败 {row['consecutive_failures']} 次：{(row['last_error'] or '')[:200]}",
                    )
                )
                continue
            stale_after = max(timedelta(minutes=cfg.interval_minutes * 4), timedelta(hours=6))
            last_ok = row["last_success_at"]
            if row["last_run_at"] and (last_ok is None or now - last_ok > stale_after):
                problems.append(
                    (
                        "source_stale",
                        row["id"],
                        f"数据源 `{row['id']}` 超过 {stale_after} 没有成功采集",
                    )
                )
            elif row["last_run_at"] and row["last_item_count"] == 0 and row["ok_runs"] >= 3:
                problems.append(
                    (
                        "source_empty",
                        row["id"],
                        f"数据源 `{row['id']}` 最近运行成功但返回 0 条，可能页面结构变化",
                    )
                )
        cost = queries.llm_cost(s, now - timedelta(hours=24), now)
    cny = float(cost["cost_usd"]) * settings.usd_to_cny
    if settings.llm_daily_budget_cny and cny >= settings.llm_daily_budget_cny * 0.8:
        problems.append(
            (
                "llm_budget",
                now.date().isoformat(),
                f"24 小时 LLM 花费 ¥{cny:.2f}，已达预算 {settings.llm_daily_budget_cny} 的 80%",
            )
        )
    return problems


def check_and_alert(push: bool = True) -> int:
    return sum(record_alert(kind, key, msg, push=push) for kind, key, msg in find_problems())


def status_report() -> str:
    settings = get_settings()
    tz = ZoneInfo(settings.report_timezone)
    now = now_utc()
    lines = []
    with session_scope() as s:
        rows = queries.source_health(s, hours=24)
        cost = queries.llm_cost(s, now - timedelta(hours=24), now)
        counts = s.execute(
            text(
                """
                SELECT (SELECT count(*) FROM raw_items),
                       (SELECT count(*) FROM raw_items WHERE fetched_at >= now() - interval '24 hours'),
                       (SELECT count(*) FROM raw_items WHERE processed_at IS NULL),
                       (SELECT count(*) FROM topics WHERE status = 'active'),
                       (SELECT count(*) FROM opportunities WHERE created_at >= now() - interval '24 hours'),
                       (SELECT count(*) FROM alerts WHERE ts >= now() - interval '24 hours')
                """
            )
        ).one()
    lines.append(
        f"条目 {counts[0]}（24h 新增 {counts[1]}，待处理 {counts[2]}） · 活跃话题 {counts[3]} · "
        f"24h 商机 {counts[4]} · 24h 告警 {counts[5]}"
    )
    lines.append(
        f"24h LLM：{cost['calls']} 次调用，{cost['tokens']} tokens，"
        f"约 ¥{float(cost['cost_usd']) * settings.usd_to_cny:.2f} / 预算 ¥{settings.llm_daily_budget_cny}"
    )
    lines.append("")
    lines.append(
        f"{'源':<22} {'状态':<6} {'24h成功':<8} {'上次条数':<8} {'上次成功（本地时间）':<20} 错误"
    )
    for r in rows:
        if not r["enabled"]:
            state = "停用"
        elif r["consecutive_failures"] >= FAIL_THRESHOLD:
            state = "故障"
        elif r["consecutive_failures"]:
            state = "波动"
        elif r["last_run_at"] is None:
            state = "未跑"
        else:
            state = "正常"
        last_ok = (
            r["last_success_at"].astimezone(tz).strftime("%m-%d %H:%M")
            if r["last_success_at"]
            else "-"
        )
        lines.append(
            f"{r['id']:<22} {state:<6} {r['ok_runs']}/{r['runs']:<6} {r['last_item_count']:<8} {last_ok:<20} "
            f"{(r['last_error'] or '')[:60]}"
        )
    return "\n".join(lines)
