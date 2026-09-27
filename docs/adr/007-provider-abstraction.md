# ADR-007 Embedding / LLM Provider 抽象

## 背景

国内外模型服务可用性、价格、合规要求不同，且变化很快。

## 决策

- `EmbeddingProvider`：`hash`（本地哈希向量，零依赖，仅同语种有效，用于开发与测试）、`api`（OpenAI 兼容接口，默认 SiliconFlow `BAAI/bge-m3`）、`local`（sentence-transformers 本地加载 bge-m3）。向量维度固定 1024。
- `LLMClient` 通过 LiteLLM 调用，分 `cheap` / `strong` 两档模型名，由环境变量配置；未配置时相应环节走启发式。
- 聚类阈值按 provider 给默认值，可用 `GS_CLUSTER_THRESHOLD` 覆盖。

## 后果

- 切换厂商只改环境变量；本机无 GPU 时默认走 API。
- 更换 embedding 模型后，历史向量需要重算（`item_embeddings.model` 记录了来源模型）。
