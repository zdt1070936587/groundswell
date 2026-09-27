# ADR-004 采集器插件协议与幂等入库

## 背景

数据源多（50+ 规划）、易失效（反爬、路由变更、限流），且各区域差异大。

## 决策

- 采集器实现 `Collector.fetch() -> list[Item]`，用 `@register("kind")` 注册；一个 kind 可在 `configs/sources.yaml` 中实例化多次。
- 入库按 `(source_id, external_id)` 幂等 upsert；同批次内重复 id 先去重，避免 `ON CONFLICT` 二次命中。
- 每次运行写 `source_runs`，并更新 `sources` 上的连续失败次数、最后成功时间与错误信息。单源失败不影响其他源。
- 源级 / 子频道级的区域归属在入库时确定；内容级归属在处理阶段完成（见 ADR-006）。
- 优先官方 API 与公开榜单：HN Algolia、GitHub Search、Reddit OAuth、Google Trends RSS、Apple 榜单 RSS；国内榜单通过自建 RSSHub 与 DailyHotApi 获取。

## 后果

- 新增数据源只需新增一个文件和一段 yaml。
- 源健康可观测，`gs status` 与钉钉告警基于这些记录。
