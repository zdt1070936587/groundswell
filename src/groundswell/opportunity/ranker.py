"""排序模型：用人工反馈（good / meh / bad）学习“哪些商机值得看”，输出 rank_score（0–10）。

特征：LLM 八维打分与总分、区域内增长指标（z / 提及 / 源数）、二次验证信号、证据与标签数量、
是否区域空白、是否启发式分析。

- 样本 < MIN_SAMPLES 或只有一种标签时不训练（沿用 LLM 总分排序）；
- 默认 numpy 逻辑回归（L2，特征标准化），零依赖、可解释；
- 样本 ≥ LGBM_MIN_SAMPLES 且安装了 lightgbm（uv sync --extra ml）时改用 LightGBM；
- 模型保存在 GS_DATA_DIR/ranker.json（LightGBM 另存 ranker.lgb.txt），夜间训练、日报前 apply。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.config import get_settings
from groundswell.db.session import session_scope
from groundswell.opportunity.schemas import DIMENSIONS
from groundswell.processing.timeutil import now_utc

MIN_SAMPLES = 20
LGBM_MIN_SAMPLES = 300
LABEL_VALUE = {"good": 1.0, "meh": 0.0, "bad": 0.0}
VALIDATION_KINDS = ("gtrends_5y", "github", "appstore", "jobs", "funding")

FEATURES = (
    ["total_score"]
    + [f"score_{d}" for d in DIMENSIONS]
    + ["max_z", "log_mentions", "sources", "n_evidence", "n_tags", "is_gap", "heuristic"]
    + [f"val_{k}" for k in VALIDATION_KINDS]
)

_FEATURE_SQL = """
SELECT o.id, o.total_score, o.scores, o.is_gap, (o.model = 'heuristic') AS heuristic,
       coalesce(array_length(o.tags, 1), 0) AS n_tags,
       (SELECT count(*) FROM evidence e WHERE e.opportunity_id = o.id) AS n_evidence,
       (SELECT jsonb_object_agg(kind, signal) FROM (
            SELECT DISTINCT ON (kind) kind, signal FROM validations v
            WHERE v.topic_id = o.topic_id AND v.region = o.region AND v.ts <= o.created_at + interval '2 days'
            ORDER BY kind, ts DESC) x) AS vals,
       st.max_z, st.mentions, st.sources
FROM opportunities o
LEFT JOIN LATERAL (
    SELECT max(z) AS max_z, sum(mentions) AS mentions, max(sources_count) AS sources
    FROM topic_hourly_stats s
    WHERE s.topic_id = o.topic_id AND s.region = o.region
      AND s.hour >= o.created_at - interval '72 hours' AND s.hour <= o.created_at
) st ON true
WHERE o.stage = 'deep' AND {where}
ORDER BY o.id
"""


def _model_path() -> Path:
    return Path(get_settings().data_dir) / "ranker.json"


def feature_row(r: dict[str, Any]) -> list[float]:
    scores = r.get("scores") or {}
    vals = r.get("vals") or {}
    row = [float(r.get("total_score") or 0.0)]
    row += [float(scores.get(d) or 0.0) for d in DIMENSIONS]
    row += [
        float(r.get("max_z") or 0.0),
        math.log1p(max(float(r.get("mentions") or 0.0), 0.0)),
        float(r.get("sources") or 0.0),
        float(r.get("n_evidence") or 0.0),
        float(r.get("n_tags") or 0.0),
        1.0 if r.get("is_gap") else 0.0,
        1.0 if r.get("heuristic") else 0.0,
    ]
    row += [float(vals.get(k) or 0.0) for k in VALIDATION_KINDS]
    return row


def load_features(session: Session, where: str, **params: Any) -> tuple[list[int], np.ndarray]:
    rows = [
        dict(r._mapping) for r in session.execute(text(_FEATURE_SQL.format(where=where)), params)
    ]
    ids = [r["id"] for r in rows]
    x = np.array([feature_row(r) for r in rows], dtype=float).reshape(len(rows), len(FEATURES))
    return ids, x


def load_labels(session: Session) -> dict[int, float]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (opportunity_id) opportunity_id, label FROM feedback
            ORDER BY opportunity_id, ts DESC
            """
        )
    ).all()
    return {oid: LABEL_VALUE[label] for oid, label in rows if label in LABEL_VALUE}


# ---------------------------------------------------------------- 逻辑回归


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_logistic(
    x: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 2000, lr: float = 0.1
) -> dict[str, Any]:
    mean = x.mean(axis=0)
    std = x.std(axis=0)
    std[std == 0] = 1.0
    xs = (x - mean) / std
    n = len(y)
    w = np.zeros(x.shape[1])
    b = 0.0
    for _ in range(iters):
        p = _sigmoid(xs @ w + b)
        grad_w = xs.T @ (p - y) / n + l2 * w / n
        grad_b = float((p - y).mean())
        w -= lr * grad_w
        b -= lr * grad_b
    return {"mean": mean.tolist(), "std": std.tolist(), "w": w.tolist(), "b": b}


def predict_logistic(model: dict[str, Any], x: np.ndarray) -> np.ndarray:
    xs = (x - np.array(model["mean"])) / np.array(model["std"])
    return _sigmoid(xs @ np.array(model["w"]) + float(model["b"]))


def auc(y: np.ndarray, p: np.ndarray) -> float | None:
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    wins = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return round(float(wins / (len(pos) * len(neg))), 3)


# ---------------------------------------------------------------- 入口


def train(min_samples: int = MIN_SAMPLES) -> dict[str, Any]:
    with session_scope() as s:
        labels = load_labels(s)
        if not labels:
            return {"status": "no_feedback", "samples": 0}
        ids, x = load_features(s, "o.id = ANY(:ids)", ids=list(labels))
    y = np.array([labels[i] for i in ids])
    out: dict[str, Any] = {"samples": len(y), "positive": int(y.sum())}
    if len(y) < min_samples or y.min() == y.max():
        out["status"] = "insufficient"
        return out
    kind = "logistic"
    model: dict[str, Any]
    if len(y) >= LGBM_MIN_SAMPLES:
        try:
            import lightgbm as lgb

            booster = lgb.train(
                {"objective": "binary", "learning_rate": 0.05, "num_leaves": 15, "verbose": -1},
                lgb.Dataset(x, label=y, feature_name=list(FEATURES)),
                num_boost_round=200,
            )
            lgb_path = _model_path().with_suffix(".lgb.txt")
            lgb_path.parent.mkdir(parents=True, exist_ok=True)
            booster.save_model(str(lgb_path))
            model, kind = {"path": lgb_path.name}, "lightgbm"
            pred = booster.predict(x)
        except ImportError:
            logger.info("未安装 lightgbm，使用逻辑回归")
    if kind == "logistic":
        model = fit_logistic(x, y)
        pred = predict_logistic(model, x)
    meta = {
        "kind": kind,
        "features": list(FEATURES),
        "trained_at": now_utc().isoformat(),
        "samples": len(y),
        "train_auc": auc(y, pred),
        "model": model,
    }
    path = _model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if kind == "logistic":
        weights = sorted(zip(FEATURES, model["w"], strict=True), key=lambda t: -abs(t[1]))
        meta["top_features"] = {k: round(v, 3) for k, v in weights[:5]}
    out.update(status="trained", kind=kind, train_auc=meta["train_auc"])
    out.update({k: meta[k] for k in ("top_features",) if k in meta})
    logger.info(f"排序模型：{out}")
    return out


def load_model() -> dict[str, Any] | None:
    path = _model_path()
    if not path.exists():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    if meta.get("features") != list(FEATURES):
        logger.warning("排序模型特征与当前代码不一致，需重新训练")
        return None
    return meta


def predict(meta: dict[str, Any], x: np.ndarray) -> np.ndarray:
    if meta["kind"] == "lightgbm":
        import lightgbm as lgb

        booster = lgb.Booster(model_file=str(_model_path().parent / meta["model"]["path"]))
        return np.asarray(booster.predict(x))
    return predict_logistic(meta["model"], x)


def apply(days: int = 7) -> int:
    meta = load_model()
    if meta is None:
        return 0
    with session_scope() as s:
        ids, x = load_features(s, "o.created_at >= now() - make_interval(days => :d)", d=days)
        if not ids:
            return 0
        scores = predict(meta, x) * 10
        s.execute(
            text("UPDATE opportunities SET rank_score = :r WHERE id = :i"),
            [{"i": i, "r": round(float(v), 3)} for i, v in zip(ids, scores, strict=True)],
        )
    return len(ids)
