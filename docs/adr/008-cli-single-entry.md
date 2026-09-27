# ADR-008 Typer CLI 作为唯一任务入口

## 背景

开发机是 Windows（无 make），部署在 Linux VPS；定时任务、手动排障、CI 需要同一套命令。

## 决策

- 所有操作都是 `gs <command>`：`db`、`config`、`collect`、`process`、`score`、`validate`、`spread`、`report`、`backtest`、`feedback`、`ideas`、`sub`、`rank`、`status`、`serve`、`worker`、`run hourly|daily|nightly`。
- 调度器（`gs serve`）与 Redis worker 只是按时间或队列调用同样的函数。
- `scripts/dev.ps1` 与 `scripts/dev.sh` 提供同样的一键开发流程；`scripts/deploy.sh`、`scripts/backup.sh` 用于 VPS。

## 后果

- 本地复现线上任务只需执行同一条命令。
- 文档只需要维护一份命令说明（README）。
