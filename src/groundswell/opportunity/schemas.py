"""商机漏斗的结构化输出。LLM 输出必须通过这些 schema 校验，否则回灌错误重试一次。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 评分维度（0–10）与总分权重
DIMENSIONS: dict[str, tuple[str, float]] = {
    "demand": ("需求强度：有多少人、多痛", 1.5),
    "growth": ("增长势头：讨论量是否在加速", 1.2),
    "willingness_to_pay": ("付费意愿：是否有人已在花钱/愿意花钱", 1.3),
    "competition_gap": ("竞争空白：现有方案越差分越高", 1.0),
    "feasibility": ("可行性：2–5 人小团队 3 个月内能否做出 MVP", 1.0),
    "distribution": ("获客路径：是否有明确、低成本的渠道", 1.0),
    "durability": ("持续性：结构性趋势高分，一次性热点低分", 1.0),
    "regional_fit": ("区域适配：支付、渠道、监管在该区域是否可行", 0.8),
}

EvidenceKind = Literal[
    "pain", "demand", "growth", "willingness_to_pay", "competitor", "regulation", "other"
]
Durability = Literal["one_off", "seasonal", "structural", "unknown"]


def total_score(scores: dict[str, float]) -> float:
    num = sum(float(scores.get(k, 0)) * w for k, (_, w) in DIMENSIONS.items())
    den = sum(w for _, w in DIMENSIONS.values())
    return round(num / den, 2)


class ScreenItem(BaseModel):
    id: int
    keep: bool
    score: float = Field(ge=0, le=10)
    reason: str = Field(default="", max_length=300)


class ScreenResult(BaseModel):
    topics: list[ScreenItem]


class EvidenceRef(BaseModel):
    item_id: int
    kind: EvidenceKind = "other"
    quote: str = Field(min_length=1, max_length=400)


class OpportunityAnalysis(BaseModel):
    is_opportunity: bool = True
    summary: str = Field(min_length=1, max_length=600)
    scores: dict[str, float]
    tags: list[str] = Field(default_factory=list, max_length=8)
    principles_applied: list[str] = Field(default_factory=list, max_length=8)
    why_now: str = Field(default="", max_length=600)
    distribution_path: str = Field(default="", max_length=600)
    product_shapes: list[str] = Field(default_factory=list, max_length=5)
    counter_arguments: list[str] = Field(default_factory=list, max_length=5)
    known_players: list[str] = Field(default_factory=list, max_length=10)
    durability: Durability = "unknown"
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=12)

    @field_validator("scores")
    @classmethod
    def _scores(cls, v: dict[str, float]) -> dict[str, float]:
        missing = [k for k in DIMENSIONS if k not in v]
        if missing:
            raise ValueError(f"scores 缺少维度 {missing}")
        return {k: max(0.0, min(10.0, float(v[k]))) for k in DIMENSIONS}
