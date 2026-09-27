"""话题归并。

- 小时级（assign_pending）：新条目与近 7 天活跃话题质心做余弦比较，超过阈值即归入并增量更新质心，
  否则新建话题。全部在内存 numpy 里完成（活跃话题一般 < 5 万，1024 维 float32 ≈ 200MB 以内）。
- 夜间（merge_similar_topics）：分块计算话题两两相似度，高于合并阈值的并查集合并；
  略低于阈值的边界对在有 LLM 时交给模型判断是否同一事件。
- 重复条目（dup_of）挂到原条目所在话题，保证转载热度计入话题。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

import numpy as np
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.db.models import EMBED_DIM, Topic
from groundswell.processing.embed import EmbeddingProvider, _normalize, _vec_literal, parse_vec

ACTIVE_DAYS = 7
PENDING_DAYS = 3
BORDERLINE = 0.06
LLM_PAIR_LIMIT = 30


def _load_topics(session: Session, model: str, days: int = ACTIVE_DAYS):
    rows = session.execute(
        text(
            """
            SELECT id, embedding, item_count FROM topics
            WHERE status = 'active' AND embedding_model = :m AND embedding IS NOT NULL
              AND last_seen >= now() - make_interval(days => :days)
            ORDER BY id
            """
        ),
        {"m": model, "days": days},
    ).all()
    ids = [r.id for r in rows]
    mat = (
        _normalize(np.stack([parse_vec(r.embedding) for r in rows]))
        if rows
        else np.zeros((0, EMBED_DIM), dtype=np.float32)
    )
    counts = np.array([max(r.item_count, 1) for r in rows], dtype=np.float32)
    return ids, mat, counts


def assign_pending(
    session: Session, provider: EmbeddingProvider, batch: int = 5000, days: int = PENDING_DAYS
) -> dict[str, int]:
    model = provider.name
    pending = session.execute(
        text(
            """
            SELECT e.item_id, e.embedding, i.title, i.fetched_at FROM item_embeddings e
            JOIN raw_items i ON i.id = e.item_id
            LEFT JOIN topic_items ti ON ti.item_id = e.item_id
            WHERE ti.item_id IS NULL AND e.model = :m AND i.dup_of IS NULL
              AND i.fetched_at >= now() - make_interval(days => :days)
            ORDER BY e.item_id LIMIT :batch
            """
        ),
        {"m": model, "days": days, "batch": batch},
    ).all()
    stats = {"assigned": 0, "new_topics": 0, "dups_attached": 0}
    if pending:
        ids, mat, counts = _load_topics(session, model)
        k0 = len(ids)
        cap = k0 + len(pending)
        C = np.zeros((cap, EMBED_DIM), dtype=np.float32)
        C[:k0] = mat
        n = np.zeros(cap, dtype=np.float32)
        n[:k0] = counts
        k = k0
        new_meta: list[tuple[str, datetime]] = []  # (title, fetched_at) for new topics
        links: list[tuple[int, int, float]] = []  # (topic_index, item_id, sim)
        thr = provider.assign_threshold
        for row in pending:
            v = parse_vec(row.embedding)
            v = v / (np.linalg.norm(v) or 1.0)
            if k:
                sims = C[:k] @ v
                j = int(np.argmax(sims))
                best = float(sims[j])
            else:
                j, best = -1, -1.0
            if best >= thr:
                c = C[j] * n[j] + v
                C[j] = c / (np.linalg.norm(c) or 1.0)
                n[j] += 1
                links.append((j, row.item_id, best))
            else:
                C[k] = v
                n[k] = 1
                new_meta.append((row.title, row.fetched_at))
                links.append((k, row.item_id, 1.0))
                k += 1

        # 新话题落库拿 id
        new_topics = [
            Topic(
                name=(title or "")[:200],
                keywords=[],
                embedding=C[k0 + i].tolist(),
                embedding_model=model,
                item_count=0,
                source_count=0,
                active_regions=[],
                slow_variables=[],
                status="active",
                first_seen=fetched,
                last_seen=fetched,
            )
            for i, (title, fetched) in enumerate(new_meta)
        ]
        session.add_all(new_topics)
        session.flush()
        index_to_id = {i: tid for i, tid in enumerate(ids)}
        index_to_id.update({k0 + i: t.id for i, t in enumerate(new_topics)})

        touched = sorted({j for j, _, _ in links if j < k0})
        if touched:
            session.execute(
                text("UPDATE topics SET embedding = CAST(:v AS vector) WHERE id = :id"),
                [{"id": index_to_id[j], "v": _vec_literal(C[j])} for j in touched],
            )
        session.execute(
            text(
                "INSERT INTO topic_items (topic_id, item_id, similarity) VALUES (:t, :i, :s) "
                "ON CONFLICT (item_id) DO NOTHING"
            ),
            [{"t": index_to_id[j], "i": item_id, "s": round(sim, 4)} for j, item_id, sim in links],
        )
        stats["assigned"] = len(links)
        stats["new_topics"] = len(new_topics)

    stats["dups_attached"] = attach_duplicates(session)
    refreshed = refresh_topics(session, since_minutes=90)
    stats["refreshed"] = refreshed
    return stats


def attach_duplicates(session: Session) -> int:
    result = session.execute(
        text(
            """
            INSERT INTO topic_items (topic_id, item_id, similarity)
            SELECT ti.topic_id, i.id, 1.0 FROM raw_items i
            JOIN topic_items ti ON ti.item_id = i.dup_of
            LEFT JOIN topic_items mine ON mine.item_id = i.id
            WHERE i.dup_of IS NOT NULL AND mine.item_id IS NULL
            ON CONFLICT (item_id) DO NOTHING
            """
        )
    )
    return result.rowcount or 0


_REFRESH_SQL = """
WITH target AS (
    SELECT DISTINCT ti.topic_id AS id FROM topic_items ti
    WHERE (CAST(:ids AS bigint[]) IS NOT NULL AND ti.topic_id = ANY(CAST(:ids AS bigint[])))
       OR (CAST(:ids AS bigint[]) IS NULL AND ti.assigned_at >= now() - make_interval(mins => :mins))
    UNION
    -- 条目被再次观测（热榜持续在榜）也要刷新 last_seen
    SELECT DISTINCT ti.topic_id FROM item_snapshots s JOIN topic_items ti ON ti.item_id = s.item_id
    WHERE CAST(:ids AS bigint[]) IS NULL AND s.ts >= now() - make_interval(mins => :mins)
), base AS (
    SELECT ti.topic_id, i.id AS item_id, i.source_id, i.fetched_at, i.last_seen_at, i.region, i.keywords
    FROM topic_items ti JOIN target t ON t.id = ti.topic_id JOIN raw_items i ON i.id = ti.item_id
), agg AS (
    SELECT topic_id, count(*) AS n, count(DISTINCT source_id) AS ns,
           min(fetched_at) AS first_seen, max(last_seen_at) AS last_seen
    FROM base GROUP BY topic_id
), kw AS (
    SELECT topic_id, (array_agg(k ORDER BY c DESC, k))[1:8] AS keywords FROM (
        SELECT topic_id, k, count(*) AS c FROM base CROSS JOIN LATERAL unnest(keywords) AS k GROUP BY 1, 2
    ) x GROUP BY topic_id
), reg AS (
    SELECT b.topic_id, array_agg(DISTINCT r.region ORDER BY r.region) AS regions
    FROM base b JOIN item_regions r ON r.item_id = b.item_id AND r.weight >= 0.3
    GROUP BY b.topic_id
), origin AS (
    SELECT DISTINCT ON (topic_id) topic_id, region FROM base
    WHERE region IS NOT NULL AND region <> 'GLOBAL'
    ORDER BY topic_id, fetched_at, item_id
)
UPDATE topics t SET
    item_count = agg.n, source_count = agg.ns, first_seen = agg.first_seen, last_seen = agg.last_seen,
    keywords = coalesce(kw.keywords, '{}'),
    active_regions = coalesce(reg.regions, '{}'),
    origin_region = coalesce(origin.region, 'GLOBAL')
FROM agg LEFT JOIN kw ON kw.topic_id = agg.topic_id LEFT JOIN reg ON reg.topic_id = agg.topic_id
LEFT JOIN origin ON origin.topic_id = agg.topic_id
WHERE t.id = agg.topic_id
"""


def refresh_topics(session: Session, ids: list[int] | None = None, since_minutes: int = 90) -> int:
    """重算话题的条目数、源数、首末时间、关键词、活跃区域、起源区域。"""
    result = session.execute(text(_REFRESH_SQL), {"ids": ids, "mins": since_minutes})
    return result.rowcount or 0


# ---------------------------------------------------------------- 合并


def merge_topics(session: Session, keep: int, others: list[int]) -> None:
    others = [o for o in others if o != keep]
    if not others:
        return
    params = {"keep": keep, "others": others}
    session.execute(
        text("UPDATE topic_items SET topic_id = :keep WHERE topic_id = ANY(:others)"), params
    )
    session.execute(text("UPDATE ideas SET topic_id = :keep WHERE topic_id = ANY(:others)"), params)
    # 历史小时统计并入保留话题（窗口内的会在下次聚合时重算）
    session.execute(
        text(
            """
            INSERT INTO topic_hourly_stats (topic_id, region, hour, mentions, new_items, engagement, sources_count)
            SELECT :keep, region, hour, sum(mentions), sum(new_items), sum(engagement), max(sources_count)
            FROM topic_hourly_stats WHERE topic_id = ANY(:others) GROUP BY region, hour
            ON CONFLICT (topic_id, region, hour) DO UPDATE SET
                mentions = topic_hourly_stats.mentions + EXCLUDED.mentions,
                new_items = topic_hourly_stats.new_items + EXCLUDED.new_items,
                engagement = topic_hourly_stats.engagement + EXCLUDED.engagement,
                sources_count = greatest(topic_hourly_stats.sources_count, EXCLUDED.sources_count)
            """
        ),
        params,
    )
    session.execute(text("DELETE FROM topic_hourly_stats WHERE topic_id = ANY(:others)"), params)
    session.execute(
        text("UPDATE topics SET status = 'merged', merged_into = :keep WHERE id = ANY(:others)"),
        params,
    )
    session.execute(
        text(
            """
            UPDATE topics t SET embedding = x.c FROM (
                SELECT avg(e.embedding) AS c FROM topic_items ti
                JOIN item_embeddings e ON e.item_id = ti.item_id
                WHERE ti.topic_id = :keep AND e.model = (SELECT embedding_model FROM topics WHERE id = :keep)
            ) x WHERE t.id = :keep AND x.c IS NOT NULL
            """
        ),
        {"keep": keep},
    )
    refresh_topics(session, ids=[keep])


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def similar_pairs(
    mat: np.ndarray, threshold: float, block: int = 1024
) -> list[tuple[int, int, float]]:
    pairs = []
    for start in range(0, len(mat), block):
        sims = mat[start : start + block] @ mat.T
        rows, cols = np.nonzero(sims >= threshold)
        for r, c in zip(rows.tolist(), cols.tolist(), strict=True):
            i = start + r
            if i < c:
                pairs.append((i, c, float(sims[r, c])))
    return pairs


def _llm_same_event(session: Session, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    from groundswell import llm

    if not pairs or not llm.available("cheap"):
        return set()
    ids = sorted({x for p in pairs for x in p})
    titles: dict[int, list[str]] = {}
    for tid, title in session.execute(
        text(
            """
            SELECT topic_id, title FROM (
                SELECT ti.topic_id, i.title, row_number() OVER (PARTITION BY ti.topic_id ORDER BY i.id) AS rn
                FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id WHERE ti.topic_id = ANY(:ids)
            ) x WHERE rn <= 3
            """
        ),
        {"ids": ids},
    ):
        titles.setdefault(tid, []).append(title[:120])
    lines = [
        f"{n}. A={titles.get(a, [])} B={titles.get(b, [])}" for n, (a, b) in enumerate(pairs, 1)
    ]
    prompt = (
        "判断每一对话题（A、B 各给出若干条代表标题）是否在讨论同一件事/同一个趋势。\n"
        + "\n".join(lines)
        + '\n\n输出 JSON：{"same": [序号, ...]}，只列出确定是同一件事的序号。'
    )
    try:
        data = llm.complete_json(
            prompt, system="你是严谨的新闻编辑，负责话题去重。", stage="merge", max_tokens=400
        )
    except llm.LLMError as exc:
        logger.warning(f"LLM 合并判断跳过：{exc}")
        return set()
    same = {int(x) for x in (data.get("same") or []) if str(x).isdigit()}
    return {pairs[i - 1] for i in same if 1 <= i <= len(pairs)}


def merge_similar_topics(
    session: Session, days: int = ACTIVE_DAYS, provider: EmbeddingProvider | None = None
) -> int:
    from groundswell.processing.embed import get_provider

    provider = provider or get_provider()
    ids, mat, counts = _load_topics(session, provider.name, days=days)
    if len(ids) < 2:
        return 0
    thr = provider.merge_threshold
    uf = _UnionFind()
    borderline: list[tuple[int, int]] = []
    for i, j, sim in similar_pairs(mat, thr - BORDERLINE):
        if sim >= thr:
            uf.union(i, j)
        elif len(borderline) < LLM_PAIR_LIMIT:
            borderline.append((ids[i], ids[j]))
    pos = {tid: n for n, tid in enumerate(ids)}
    for a, b in _llm_same_event(session, borderline):
        uf.union(pos[a], pos[b])

    groups: dict[int, list[int]] = {}
    for i in range(len(ids)):
        if i in uf.parent:
            groups.setdefault(uf.find(i), []).append(i)
    merged = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        # 保留条目最多的话题（相同则保留最早的）
        keep_idx = max(members, key=lambda m: (counts[m], -ids[m]))
        others = [ids[m] for m in members if m != keep_idx]
        merge_topics(session, ids[keep_idx], others)
        merged += len(others)
    if merged:
        logger.info(f"夜间合并 {merged} 个话题（阈值 {thr}）")
    return merged


def representative_titles(session: Session, topic_id: int, limit: int = 8) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT i.title FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id
            WHERE ti.topic_id = :t ORDER BY ti.similarity DESC, i.id LIMIT :n
            """
        ),
        {"t": topic_id, "n": limit * 2},
    ).scalars()
    seen: Counter[str] = Counter()
    out = []
    for t in rows:
        key = t.strip().lower()
        if key and not seen[key]:
            out.append(t.strip())
        seen[key] += 1
        if len(out) >= limit:
            break
    return out
