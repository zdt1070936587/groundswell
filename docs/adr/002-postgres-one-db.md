# ADR-002 PostgreSQL 一库多用

## 背景

系统同时需要：关系数据（源、话题、商机、点子）、时序计数（每小时热度）、向量相似度（话题归并与历史相似话题）、半结构化指标（各平台互动数据格式不一）。

## 决策

使用镜像 `timescale/timescaledb-ha:pg16`，它同时内置 TimescaleDB 与 pgvector：

- `item_snapshots`、`keyword_hourly_stats`、`topic_hourly_stats` 为 hypertable，按 7 天分块，超过 14/30 天自动压缩；
- `topic_daily_stats` 为连续聚合，供日报、周报和前端使用；
- `topics.embedding` 使用 HNSW 索引（cosine）；
- 各平台互动数据存 JSONB。

## 后果

- 一套事务、一套备份（`pg_dump`）、SQL 可以跨关系/时序/向量联查。
- 单机容量上限约为日均百万级条目，超过后按 ADR-009 迁移计数到 ClickHouse。
- hypertable 关闭默认时间索引（与复合主键重复），以保证 `alembic check` 与模型完全一致。
