"""方法论库：configs/methodology.yaml 是唯一事实来源，同步到 methodology 表供查询与回溯。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from groundswell.configs import MethodologyFile, Principle, load_methodology
from groundswell.db.models import MethodologyPrinciple
from groundswell.processing.terms import matches_any


def sync_methodology(session: Session, methodology: MethodologyFile | None = None) -> str:
    m = methodology or load_methodology()
    existing = {p.id: p for p in session.scalars(select(MethodologyPrinciple))}
    for p in m.principles:
        row = existing.pop(p.id, None) or MethodologyPrinciple(id=p.id)
        row.person = p.person
        row.principle = p.principle
        row.question = p.question
        row.tag = p.tag
        row.weight = p.weight
        row.region_specific = p.region_specific
        row.signals = list(p.signals)
        row.version = m.version
        row.active = True
        session.add(row)
    for row in existing.values():
        row.active = False
    return m.version


def principles_text(principles: list[Principle]) -> str:
    return "\n".join(
        f"- [{p.id}] ({p.person}·{p.principle}) {p.question} → tag={p.tag}" for p in principles
    )


def match_signals(text: str, principles: list[Principle]) -> list[Principle]:
    """启发式模式：正文命中 signals 的方法论。"""
    return [p for p in principles if p.signals and matches_any(text, p.signals)]


def tag_weight(tags: list[str], principles: list[Principle]) -> float:
    weights = {p.tag: p.weight for p in principles}
    return sum(weights.get(t, 0.0) for t in set(tags))
