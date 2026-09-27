"""Embedding 提供方。三种实现可互换，向量统一 L2 归一化、维度 EMBED_DIM（1024）。

- hash：特征哈希（字 bigram + 词），无需密钥，仅同语种近似有效，开发/兜底用；
- api：OpenAI 兼容 /embeddings 接口，默认 SiliconFlow 的 BAAI/bge-m3（多语种，跨语言同义可聚到一起）；
- local：sentence-transformers 本地加载 bge-m3。

不同模型的向量不可比：topics.embedding_model 记录来源，归并只在同模型内进行。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx
import numpy as np
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.config import Settings, get_settings
from groundswell.db.models import EMBED_DIM
from groundswell.processing.dedupe import features


class EmbeddingProvider(Protocol):
    name: str
    dim: int
    #: 条目归入话题的余弦阈值
    assign_threshold: float
    #: 夜间话题合并阈值
    merge_threshold: float

    def embed(self, texts: list[str]) -> np.ndarray: ...


def _normalize(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (m / norms).astype(np.float32)


def _fit_dim(m: np.ndarray, dim: int = EMBED_DIM) -> np.ndarray:
    if m.shape[1] == dim:
        return m
    if m.shape[1] > dim:
        return m[:, :dim]
    return np.pad(m, ((0, 0), (0, dim - m.shape[1])))


@dataclass
class HashingEmbedding:
    name: str = "hash-v1"
    dim: int = EMBED_DIM
    # 短标题共享一个词（如 "Google X"）时质心会逐步漂移串联，阈值需偏高
    assign_threshold: float = 0.6
    merge_threshold: float = 0.75

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for f in features(t):
                h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
                sign = 1.0 if (h >> 63) & 1 else -1.0
                # 词 / 词组比单字 bigram 更有区分度
                weight = 1.5 if " " in f or f.isascii() else 1.0
                out[i, h % self.dim] += sign * weight
        return _normalize(out)


@dataclass
class ApiEmbedding:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60
    batch_size: int = 32
    dim: int = EMBED_DIM
    assign_threshold: float = 0.72
    merge_threshold: float = 0.82

    @property
    def name(self) -> str:
        return self.model[:64]

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs: list[list[float]] = []
        with httpx.Client(timeout=self.timeout) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = [t[:2000] or " " for t in texts[start : start + self.batch_size]]
                payload: dict = {"model": self.model, "input": batch, "encoding_format": "float"}
                if self.model.startswith("text-embedding-3"):
                    payload["dimensions"] = self.dim
                data = self._post(client, payload)
                rows = sorted(data["data"], key=lambda d: d["index"])
                vecs.extend(r["embedding"] for r in rows)
                _record_usage(self.name, int(data.get("usage", {}).get("total_tokens", 0)))
        return _normalize(_fit_dim(np.asarray(vecs, dtype=np.float32), self.dim))

    def _post(self, client: httpx.Client, payload: dict) -> dict:
        url = self.base_url.rstrip("/") + "/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(4):
            try:
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise
                wait = 2**attempt
                logger.warning(f"embedding 请求失败（{exc}），{wait}s 后重试")
                time.sleep(wait)
        raise RuntimeError("unreachable")


@dataclass
class LocalEmbedding:
    model: str = "BAAI/bge-m3"
    dim: int = EMBED_DIM
    assign_threshold: float = 0.72
    merge_threshold: float = 0.82

    @property
    def name(self) -> str:
        return f"local:{self.model}"[:64]

    def embed(self, texts: list[str]) -> np.ndarray:
        model = _load_st(self.model)
        m = model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        return _normalize(_fit_dim(np.asarray(m, dtype=np.float32), self.dim))


@lru_cache(maxsize=2)
def _load_st(name: str):
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def _record_usage(model: str, tokens: int) -> None:
    if not tokens:
        return
    from groundswell.db.models import LlmUsage
    from groundswell.db.session import session_scope

    with session_scope() as s:
        s.add(
            LlmUsage(
                model=model, stage="embed", prompt_tokens=tokens, completion_tokens=0, cost_usd=0.0
            )
        )


def get_provider(settings: Settings | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    kind = settings.embedding_provider
    provider: EmbeddingProvider
    if kind == "api":
        if not settings.embedding_api_key:
            logger.warning("GS_EMBEDDING_PROVIDER=api 但未配置 GS_EMBEDDING_API_KEY，降级为 hash")
            provider = HashingEmbedding()
        else:
            provider = ApiEmbedding(
                settings.embedding_api_base, settings.embedding_api_key, settings.embedding_model
            )
    elif kind == "local":
        provider = LocalEmbedding(settings.embedding_model)
    else:
        provider = HashingEmbedding()
    if settings.cluster_threshold:
        provider.assign_threshold = settings.cluster_threshold
    if settings.merge_threshold:
        provider.merge_threshold = settings.merge_threshold
    return provider


def item_text(title: str, body: str | None) -> str:
    """标题足够长时只用标题：正文常含模板/导语等跨条目共有的噪声。"""
    if not body or len(title) >= 40:
        return title
    return f"{title}\n{body[:200]}"


def embed_pending(
    session: Session, provider: EmbeddingProvider, batch: int = 256, days: int = 3
) -> int:
    """为已标注、非重复、尚无（当前模型）向量的近期条目生成 embedding。"""
    rows = session.execute(
        text(
            """
            SELECT i.id, i.title, i.text FROM raw_items i
            LEFT JOIN item_embeddings e ON e.item_id = i.id
            WHERE i.processed_at IS NOT NULL AND i.dup_of IS NULL
              AND i.fetched_at >= now() - make_interval(days => :days)
              AND (e.item_id IS NULL OR e.model <> :model)
            ORDER BY i.id LIMIT :batch
            """
        ),
        {"days": days, "model": provider.name, "batch": batch},
    ).all()
    if not rows:
        return 0
    vecs = provider.embed([item_text(r.title, r.text) for r in rows])
    session.execute(
        text(
            """
            INSERT INTO item_embeddings (item_id, model, embedding)
            VALUES (:id, :model, CAST(:vec AS vector))
            ON CONFLICT (item_id) DO UPDATE SET model = EXCLUDED.model, embedding = EXCLUDED.embedding,
                created_at = now()
            """
        ),
        [
            {"id": r.id, "model": provider.name, "vec": _vec_literal(v)}
            for r, v in zip(rows, vecs, strict=True)
        ],
    )
    return len(rows)


def _vec_literal(v: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v.tolist()) + "]"


def parse_vec(value) -> np.ndarray:
    """pgvector 列在原生 SQL 查询中返回字符串或 ndarray。"""
    if isinstance(value, np.ndarray):
        return value.astype(np.float32)
    if isinstance(value, str):
        return np.asarray([float(x) for x in value.strip("[]").split(",")], dtype=np.float32)
    return np.asarray(value, dtype=np.float32)
