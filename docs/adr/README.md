# 架构决策记录（ADR）

每条记录包含：背景、决策、后果。决策被推翻时不删除原记录，而是新增一条并标注“取代 ADR-xxx”。

| 编号 | 标题 | 状态 |
|---|---|---|
| [001](001-modular-monolith-hourly-batch.md) | 模块化单体 + 小时级批处理 | 已采纳 |
| [002](002-postgres-one-db.md) | PostgreSQL 一库多用（Timescale + pgvector） | 已采纳 |
| [003](003-immutable-raw-rebuildable-derived.md) | 原始数据不可变，派生数据可重建 | 已采纳 |
| [004](004-collector-plugin-protocol.md) | 采集器插件协议与幂等入库 | 已采纳 |
| [005](005-llm-funnel-evidence.md) | LLM 分层漏斗与证据强制 | 已采纳 |
| [006](006-region-dimension.md) | region 维度从第一天进入主键 | 已采纳 |
| [007](007-provider-abstraction.md) | Embedding / LLM Provider 抽象 | 已采纳 |
| [008](008-cli-single-entry.md) | Typer CLI 作为唯一任务入口 | 已采纳 |
| [009](009-data-layer-evolution.md) | 数据层演进触发条件 | 已采纳（规划） |
| [010](010-productization-reserve.md) | 产品化预留：鉴权、多租户、计费 | 已采纳（规划） |
