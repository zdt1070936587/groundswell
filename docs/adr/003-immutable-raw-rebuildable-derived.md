# ADR-003 原始数据不可变，派生数据可重建

## 背景

方法论、prompt、聚类阈值、趋势参数都会反复迭代。如果每次迭代只能影响未来数据，就无法客观比较新旧版本，也无法回测。

## 决策

- `raw_items` 的内容字段（标题、正文、链接、发布时间）写入后不再修改；upsert 只刷新观测字段（最新互动数据、排名、last_seen_at），每次观测另写一条 `item_snapshots`。
- 标注字段（语言、区域、关键词、simhash、dup_of）以及所有其他表都是派生数据，删除后可以重新计算。
- 商机记录保存 `prompt_version`、`methodology_version`、`model` 与 `input_hash`，不同版本的结果并存。
- `gs backtest` 基于原始数据重放，输出命中率与领先天数；`gs data import/export` 支持导入历史数据。

## 后果

- 任何参数改动都能在历史数据上复算并对比，方法论是否有效有了客观标准。
- 存储会随时间增长；hypertable 压缩与 ADR-009 的迁移策略负责控制成本。
