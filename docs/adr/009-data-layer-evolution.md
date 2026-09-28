# ADR-009 数据层演进触发条件

## 背景

ADR-002 选择 PostgreSQL 一库多用（TimescaleDB 时序 + pgvector 向量 + 普通关系表）。单机 VPS、日增量十万级条目时这是成本最低、运维最简单的方案；但数据量或查询模式变化后，个别负载会先于其他负载触到瓶颈。提前约定“什么时候换、换什么、怎么换”，避免过早优化，也避免临时救火。

## 决策

默认不引入新存储。以下任一指标连续一周超过阈值时，才对**对应负载**做拆分，其余部分保持不变：

| 负载 | 触发条件 | 演进方向 | 迁移方式 |
|---|---|---|---|
| 计数 / 时序聚合（`*_hourly_stats`、`item_snapshots`） | 小时聚合任务 > 10 分钟，或 hypertable 压缩后 > 200 GB | ClickHouse（MergeTree 按 `region, hour` 排序） | `gs data export` 导出 JSONL → ClickHouse 导入；统计 SQL 已集中在 `processing/trend.py` 与 `queries.py`，只需替换这两处 |
| 全文检索（`/api/search`、证据检索） | `ILIKE` 查询 P95 > 1 s，或需要分词 / 拼写容错 | Meilisearch（单二进制，按 `raw_items` 同步） | 新增同步任务读取 `raw_items`，写入索引；原始表不变 |
| 向量（`item_embeddings`、话题归并） | 近 3 天向量 > 500 万，或 HNSW 召回 P95 > 200 ms | pgvector 分区 / 半精度 → 独立向量库（Qdrant） | `EmbeddingProvider` 与 `cluster.py` 只依赖“按 id 取向量 / 近邻查询”两个操作，替换实现即可 |
| Embedding 计算 | API 成本 > 每日 LLM 预算的 30%，或延迟阻塞小时任务 | 本地 GPU 跑 bge-m3（`GS_EMBEDDING_PROVIDER=local`） | 已有 `LocalEmbedding`；换模型需重算向量（`item_embeddings.model` 记录来源） |
| 任务编排 | 定时任务 > 10 个或需要重试 / 依赖图 / 回填 UI | Prefect 或 Dagster 替换 APScheduler | 任务入口统一在 `pipeline.py` / `gs run *`（ADR-008），编排器只调用这些函数 |

## 不变量

- `raw_items` 始终是唯一事实来源（ADR-003）：任何新存储都是可以从 `raw_items` 重建的派生物。
- 区域维度随数据一起迁移（ADR-006）：新存储的主键 / 排序键必须包含 `region`。
- 回测（`gs backtest`）与线上共用同一套打分函数，迁移前后回测 KPI 必须一致，作为迁移验收标准。

## 后果

- 一段时间内只维护一个数据库，备份 / 恢复只有 `pg_dump` 一条路径。
- 真正需要拆分时，拆分范围小、可逐项验收；代价是需要持续观察上表中的指标（`gs status` 与源健康表已覆盖任务耗时，其余靠 PostgreSQL 慢查询日志）。
