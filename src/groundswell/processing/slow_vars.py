"""慢变量标注：把快速话题挂到长期结构性趋势（老龄化、出海、AI 替代白领……）上。

慢变量决定一个热点是“一次性”还是“结构性”；商机漏斗会把命中的慢变量作为 why_now 的证据之一。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.configs import SlowVariablesFile, load_slow_variables
from groundswell.processing.terms import matches_any


def match_slow_variables(body: str, variables: SlowVariablesFile) -> list[str]:
    return [v.id for v in variables.variables if matches_any(body, v.keywords)]


def tag_slow_variables(session: Session, days: int = 2) -> int:
    variables = load_slow_variables()
    if not variables.variables:
        return 0
    rows = session.execute(
        text(
            """
            SELECT id, name, summary, keywords, slow_variables FROM topics
            WHERE status = 'active' AND last_seen >= now() - make_interval(days => :days)
            """
        ),
        {"days": days},
    ).all()
    updates = []
    for r in rows:
        body = " ".join([r.name or "", r.summary or "", " ".join(r.keywords or [])])
        matched = match_slow_variables(body, variables)
        if sorted(matched) != sorted(r.slow_variables or []):
            updates.append({"id": r.id, "v": matched})
    if updates:
        session.execute(text("UPDATE topics SET slow_variables = :v WHERE id = :id"), updates)
    return len(updates)
