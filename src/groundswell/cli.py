"""命令行入口：gs <command>。所有定时任务、手动操作、运维命令都从这里进入。"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import typer

from groundswell.logging import setup_logging

app = typer.Typer(no_args_is_help=True, help="观潮 Groundswell：多区域舆情 → 话题 → 商机")
db_app = typer.Typer(no_args_is_help=True, help="数据库迁移")
config_app = typer.Typer(no_args_is_help=True, help="配置校验")
topics_app = typer.Typer(no_args_is_help=True, help="话题维护")
report_app = typer.Typer(no_args_is_help=True, help="报告")
data_app = typer.Typer(no_args_is_help=True, help="数据导入导出")
rank_app = typer.Typer(no_args_is_help=True, help="排序模型")
ideas_app = typer.Typer(no_args_is_help=True, help="点子库")
sub_app = typer.Typer(no_args_is_help=True, help="订阅与告警")
reviews_app = typer.Typer(no_args_is_help=True, help="差评挖掘")
run_app = typer.Typer(no_args_is_help=True, help="手动触发定时流水线")

for sub, name in (
    (db_app, "db"),
    (config_app, "config"),
    (topics_app, "topics"),
    (report_app, "report"),
    (data_app, "data"),
    (rank_app, "rank"),
    (ideas_app, "ideas"),
    (sub_app, "sub"),
    (reviews_app, "reviews"),
    (run_app, "run"),
):
    app.add_typer(sub, name=name)


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="输出 DEBUG 日志")) -> None:
    setup_logging("DEBUG" if verbose else None)


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()] if value else []


# ---------------------------------------------------------------- db / config


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    """迁移到指定版本（默认 head）。"""
    from groundswell.db import migrate

    migrate.upgrade(revision)
    typer.echo(f"已迁移到 {revision}")


@db_app.command("downgrade")
def db_downgrade(revision: str) -> None:
    from groundswell.db import migrate

    migrate.downgrade(revision)


@db_app.command("current")
def db_current() -> None:
    from groundswell.db import migrate

    migrate.current()


@db_app.command("check")
def db_check() -> None:
    """模型与数据库结构一致性检查（等价于 alembic check）。"""
    from groundswell.db import migrate

    migrate.check()
    typer.echo("模型与数据库一致")


@config_app.command("check")
def config_check() -> None:
    """交叉校验 configs/*.yaml。"""
    from groundswell.configs import check_all, load_methodology, load_regions, load_sources

    problems = check_all()
    if problems:
        for p in problems:
            typer.secho(f"  ✗ {p}", fg="red")
        raise typer.Exit(1)
    regions, sources = load_regions(), load_sources()
    enabled = [s for s in sources.sources if s.enabled]
    typer.secho(
        f"配置通过：{len(regions.regions)} 个区域，{len(sources.sources)} 个数据源（启用 {len(enabled)}），"
        f"方法论 {len(load_methodology().principles)} 条",
        fg="green",
    )


# ---------------------------------------------------------------- 采集 / 处理


@app.command()
def collect(
    sources: list[str] = typer.Argument(None, help="数据源 id；缺省为全部启用的源"),
    due: bool = typer.Option(False, help="只跑到期的源（调度器使用）"),
) -> None:
    """抓取数据源并入库。"""
    from groundswell.collectors.runner import collect as run_collect

    results = run_collect(sources or None, only_due=due)
    ok = sum(r.ok for r in results)
    for r in results:
        mark = typer.style("ok  ", fg="green") if r.ok else typer.style("FAIL", fg="red")
        typer.echo(
            f"  {mark} {r.source_id:<22} items={r.items:<4} new={r.new_items:<4} {r.duration_ms}ms {r.error or ''}"
        )
    typer.echo(f"{ok}/{len(results)} 个源成功")
    if results and ok == 0:
        raise typer.Exit(1)


@app.command()
def process(
    stage: str = typer.Option("all", help="annotate | keywords | embed | cluster | topics | all"),
    hours: int = typer.Option(3, help="重算最近 N 小时的统计"),
) -> None:
    """处理流水线：标注（语言/区域/关键词/去重）→ 关键词统计 → embedding → 归并 → 话题统计。"""
    from groundswell import pipeline

    stats = pipeline.process(stage=stage, hours=hours)
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


@topics_app.command("recluster")
def topics_recluster(days: int = 7) -> None:
    """夜间任务：合并近义话题。"""
    from groundswell.db.session import session_scope
    from groundswell.processing.cluster import merge_similar_topics

    with session_scope() as s:
        typer.echo(f"合并了 {merge_similar_topics(s, days=days)} 个话题")


@topics_app.command("name")
def topics_name(limit: int = 100) -> None:
    """为话题生成名称、摘要与分类（有 LLM 用 LLM，否则启发式）。"""
    from groundswell.db.session import session_scope
    from groundswell.processing.naming import name_topics

    with session_scope() as s:
        typer.echo(f"命名 {name_topics(s, limit=limit)} 个话题")


@topics_app.command("merge")
def topics_merge(keep: int, others: list[int]) -> None:
    """人工合并话题：gs topics merge <保留id> <被合并id...>"""
    from groundswell.db.session import session_scope
    from groundswell.processing.cluster import merge_topics

    with session_scope() as s:
        merge_topics(s, keep, others)
    typer.echo(f"已将 {others} 合并到 {keep}")


@topics_app.command("show")
def topics_show(topic_id: int) -> None:
    from groundswell import queries
    from groundswell.db.session import session_scope

    with session_scope() as s:
        detail = queries.topic_detail(s, topic_id)
    typer.echo(json.dumps(detail, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------- 商机


@app.command()
def score(
    regions: str = typer.Option(None, help="逗号分隔的区域，缺省为全部"),
    limit: int = typer.Option(None, help="深度分析上限，缺省取 GS_LLM_DEEP_DAILY_CAP"),
) -> None:
    """商机漏斗：候选筛选 → 初筛 → 深度分析（按区域分别生成）。"""
    from groundswell.opportunity.funnel import run_funnel

    stats = run_funnel(regions=_csv(regions) or None, deep_limit=limit)
    typer.echo(json.dumps(stats, ensure_ascii=False, indent=2))


@app.command()
def validate(days: int = 1, limit: int = 30) -> None:
    """对最近的商机跑二次验证（Trends 5 年线、GitHub、App Store、招聘、融资）。"""
    from groundswell.opportunity.validators import run_validations

    typer.echo(json.dumps(run_validations(days=days, limit=limit), ensure_ascii=False, indent=2))


@app.command()
def spread(days: int = 14, gaps: bool = typer.Option(True, help="同时对区域空白跑分析")) -> None:
    """计算跨区扩散路径并检测区域空白（信息差）。"""
    from groundswell.opportunity.spread import run_spread

    typer.echo(json.dumps(run_spread(days=days, analyze_gaps=gaps), ensure_ascii=False, indent=2))


@app.command()
def feedback(
    opportunity_id: int,
    label: str = typer.Argument(..., help="good | meh | bad"),
    note: str = typer.Option(None),
    user: str = typer.Option("me"),
) -> None:
    """人工反馈，用于训练排序模型；good 会自动加入点子库。"""
    from groundswell.ideas import record_feedback

    record_feedback(opportunity_id, label, note=note, user=user)
    typer.echo(f"已记录 #{opportunity_id} → {label}")


# ---------------------------------------------------------------- 报告


@report_app.command("daily")
def report_daily(
    day: str = typer.Option(None, "--date", help="YYYY-MM-DD，缺省为今天（报告时区）"),
    push: bool = typer.Option(False, help="推送到钉钉"),
    regions: str = typer.Option(None, help="逗号分隔，缺省取 GS_REPORT_REGIONS"),
) -> None:
    from groundswell.reporting.daily import build_and_save_daily

    path, text = build_and_save_daily(
        day=date.fromisoformat(day) if day else None, regions=_csv(regions) or None, push=push
    )
    typer.echo(text)
    typer.secho(f"\n已写入 {path}", fg="green")


@report_app.command("weekly")
def report_weekly(push: bool = False) -> None:
    from groundswell.reporting.periodic import build_and_save_periodic

    path, text = build_and_save_periodic("weekly", push=push)
    typer.echo(text)
    typer.secho(f"\n已写入 {path}", fg="green")


@report_app.command("monthly")
def report_monthly(push: bool = False) -> None:
    from groundswell.reporting.periodic import build_and_save_periodic

    path, text = build_and_save_periodic("monthly", push=push)
    typer.echo(text)
    typer.secho(f"\n已写入 {path}", fg="green")


@report_app.command("brief")
def report_brief(opportunity_id: int) -> None:
    """为单个商机生成 Trends.vc 风格报告 + 一页纸商业计划草稿。"""
    from groundswell.reporting.brief import build_brief

    path, text = build_brief(opportunity_id)
    typer.echo(text)
    typer.secho(f"\n已写入 {path}", fg="green")


# ---------------------------------------------------------------- 回测 / 数据


@app.command()
def backtest(
    golden: Path = typer.Option(None, help="黄金集路径，缺省 configs/golden_set.yaml"),
    lookback_days: int = typer.Option(120, help="每个案例回看 breakout 前多少天"),
    out: Path = typer.Option(None, help="报告输出目录，缺省 reports/backtest"),
) -> None:
    """回测：检验系统能否在已知成功案例起飞前发出信号。"""
    from groundswell.backtest import run_backtest

    path, summary = run_backtest(golden_path=golden, lookback_days=lookback_days, out_dir=out)
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    typer.secho(f"\n报告 {path}", fg="green")


@data_app.command("import")
def data_import(
    path: Path,
    source: str = typer.Option("import", help="导入数据挂到的 source id"),
    region: str = typer.Option("GLOBAL"),
) -> None:
    """导入历史数据（JSONL：title, published_at, 可选 url/text/country/language/metrics/external_id）。"""
    from groundswell.dataio import import_jsonl

    typer.echo(f"导入 {import_jsonl(path, source_id=source, region=region)} 条")


@data_app.command("export")
def data_export(path: Path, days: int = 30) -> None:
    """导出最近 N 天 raw_items 为 JSONL（迁移 ClickHouse / 离线分析 / 回测共享）。"""
    from groundswell.dataio import export_jsonl

    typer.echo(f"导出 {export_jsonl(path, days=days)} 条 → {path}")


# ---------------------------------------------------------------- 排序 / 点子库 / 订阅


@rank_app.command("train")
def rank_train() -> None:
    from groundswell.opportunity.ranker import train

    typer.echo(json.dumps(train(), ensure_ascii=False, indent=2))


@rank_app.command("apply")
def rank_apply(days: int = 7) -> None:
    from groundswell.opportunity.ranker import apply

    typer.echo(f"更新 {apply(days=days)} 条 rank_score")


@ideas_app.command("list")
def ideas_list(status: str = typer.Option(None)) -> None:
    from groundswell.ideas import list_ideas

    for idea in list_ideas(status):
        typer.echo(
            f"#{idea['id']:<5} [{idea['status']:<9}] {idea['region']:<6} {idea['title']}  "
            f"owner={idea['owner'] or '-'}"
        )


@ideas_app.command("add")
def ideas_add(opportunity_id: int, user: str = "me") -> None:
    from groundswell.ideas import add_from_opportunity

    typer.echo(f"点子 #{add_from_opportunity(opportunity_id, user=user)}")


@ideas_app.command("move")
def ideas_move(idea_id: int, status: str, note: str = typer.Option(None), user: str = "me") -> None:
    """candidate → watching → validated → archived"""
    from groundswell.ideas import move_idea

    move_idea(idea_id, status, note=note, user=user)
    typer.echo(f"#{idea_id} → {status}")


@ideas_app.command("assign")
def ideas_assign(idea_id: int, owner: str) -> None:
    from groundswell.ideas import assign_idea

    assign_idea(idea_id, owner)
    typer.echo(f"#{idea_id} 负责人 → {owner}")


@ideas_app.command("comment")
def ideas_comment(idea_id: int, body: str, user: str = "me") -> None:
    from groundswell.ideas import add_comment

    add_comment(idea_id, body, user=user)
    typer.echo("已评论")


@sub_app.command("add")
def sub_add(
    name: str,
    keywords: str = typer.Option(..., help="逗号分隔"),
    regions: str = typer.Option(None, help="逗号分隔，缺省全部"),
    categories: str = typer.Option(None, help="逗号分隔，缺省全部"),
    min_score: float = 2.0,
    channel: str = typer.Option("default", help="default=钉钉默认机器人，或自定义 webhook URL"),
) -> None:
    from groundswell.subscriptions import add_subscription

    sid = add_subscription(
        name, _csv(keywords), _csv(regions), _csv(categories), min_score, channel
    )
    typer.echo(f"订阅 #{sid}")


@sub_app.command("list")
def sub_list() -> None:
    from groundswell.subscriptions import list_subscriptions

    for s in list_subscriptions():
        typer.echo(json.dumps(s, ensure_ascii=False, default=str))


@sub_app.command("rm")
def sub_rm(subscription_id: int) -> None:
    from groundswell.subscriptions import remove_subscription

    remove_subscription(subscription_id)
    typer.echo("已删除")


@reviews_app.command("mine")
def reviews_mine(
    app_id: str = typer.Argument(..., help="App Store 应用 id"),
    country: str = typer.Option("us"),
    pages: int = typer.Option(3, help="每页 50 条，最多 10 页"),
    max_rating: int = typer.Option(2, help="只分析不高于该星级的评论"),
) -> None:
    """抓取应用差评并按痛点聚类，输出 Markdown 报告。"""
    from groundswell.reviews import mine_app_reviews

    path, text = mine_app_reviews(app_id, country=country, pages=pages, max_rating=max_rating)
    typer.echo(text)
    typer.secho(f"\n已写入 {path}", fg="green")


# ---------------------------------------------------------------- 运行 / 运维


@run_app.command("hourly")
def run_hourly() -> None:
    from groundswell import pipeline

    typer.echo(json.dumps(pipeline.run_hourly(), ensure_ascii=False, indent=2, default=str))


@run_app.command("daily")
def run_daily(push: bool = True) -> None:
    from groundswell import pipeline

    typer.echo(json.dumps(pipeline.run_daily(push=push), ensure_ascii=False, indent=2, default=str))


@run_app.command("nightly")
def run_nightly() -> None:
    from groundswell import pipeline

    typer.echo(json.dumps(pipeline.run_nightly(), ensure_ascii=False, indent=2, default=str))


@app.command()
def status() -> None:
    """各源新鲜度、24 小时成功率、昨日 LLM 成本。"""
    from groundswell.ops.health import status_report

    typer.echo(status_report())


@app.command()
def serve(
    api: bool = typer.Option(True, help="启动 FastAPI"),
    scheduler: bool = typer.Option(True, help="启动内置调度器"),
) -> None:
    """常驻服务：APScheduler 定时任务 + FastAPI。"""
    from groundswell.scheduler import serve as run_serve

    run_serve(api=api, scheduler=scheduler)


@app.command()
def worker() -> None:
    """Redis 队列 worker（GS_REDIS_URL 必填），可多实例水平扩展。"""
    from groundswell.taskqueue import run_worker

    run_worker()


@app.command()
def version() -> None:
    from groundswell import __version__

    typer.echo(f"groundswell {__version__} ({datetime.now(UTC):%Y-%m-%d})")


if __name__ == "__main__":
    app()
