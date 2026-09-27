# ADR-001 模块化单体 + 小时级批处理

## 背景

团队 1–3 人，日均数据量 2–5 万条，远期 10–50 万条。舆情热度的有效粒度是小时：榜单本身按分钟到小时刷新，商机判断以天为单位。

## 决策

- 单个 Python 包 `groundswell`，按层分模块（collectors / processing / opportunity / reporting / api），模块间只通过数据库表和纯函数交互。
- 处理以小时为节拍批量运行（`gs run hourly`），不引入 Kafka / Flink 等流式组件。
- 同名阶段用 PostgreSQL advisory lock 互斥，调度器、worker、手动 CLI 可以安全并存。

## 后果

- 一个 Compose 文件、一个进程即可运行全部功能，部署与排障成本最低。
- 最低延迟约 1 小时，对“发现商机”足够；若将来需要分钟级告警，只需把订阅匹配单独提频。
- 模块边界清晰，未来可以把采集拆成独立 worker（已在 Phase 5 通过 Redis 队列实现）而不改其他模块。
