"""人工反馈与点子库生命周期：candidate → watching → validated → archived。

反馈 good 自动把商机加入点子库；所有状态变更写入 idea_events，作为排序模型之外的第二份“人类判断”数据。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from groundswell.db.models import Feedback, Idea, IdeaComment, IdeaEvent, Opportunity, Topic
from groundswell.db.session import session_scope
from groundswell.taxonomy import FEEDBACK_LABELS, IDEA_STATUSES


def _idea_title(opp: Opportunity, topic: Topic | None) -> str:
    name = topic.name if topic else f"话题 #{opp.topic_id}"
    shape = ""
    if opp.product_shapes:
        first = opp.product_shapes[0]
        shape = (
            first if isinstance(first, str) else str(first.get("name") or first.get("shape") or "")
        )
    return f"{name}：{shape}"[:300] if shape else name[:300]


def _ensure_idea(session, opp: Opportunity, user: str) -> int:
    existing = session.scalar(select(Idea).where(Idea.opportunity_id == opp.id))
    if existing:
        return existing.id
    topic = session.get(Topic, opp.topic_id)
    idea = Idea(
        opportunity_id=opp.id,
        topic_id=opp.topic_id,
        region=opp.region,
        title=_idea_title(opp, topic),
        status="candidate",
    )
    session.add(idea)
    session.flush()
    session.add(
        IdeaEvent(
            idea_id=idea.id,
            from_status=None,
            to_status="candidate",
            note="加入点子库",
            user_name=user,
        )
    )
    return idea.id


def record_feedback(
    opportunity_id: int, label: str, note: str | None = None, user: str = "me"
) -> int | None:
    if label not in FEEDBACK_LABELS:
        raise ValueError(f"label 必须是 {FEEDBACK_LABELS} 之一")
    with session_scope() as s:
        opp = s.get(Opportunity, opportunity_id)
        if opp is None:
            raise ValueError(f"商机 #{opportunity_id} 不存在")
        s.add(Feedback(opportunity_id=opportunity_id, label=label, note=note, user_name=user))
        if label == "good":
            return _ensure_idea(s, opp, user)
    return None


def add_from_opportunity(opportunity_id: int, user: str = "me") -> int:
    with session_scope() as s:
        opp = s.get(Opportunity, opportunity_id)
        if opp is None:
            raise ValueError(f"商机 #{opportunity_id} 不存在")
        return _ensure_idea(s, opp, user)


def _as_dict(idea: Idea) -> dict[str, Any]:
    return {
        "id": idea.id,
        "opportunity_id": idea.opportunity_id,
        "topic_id": idea.topic_id,
        "region": idea.region,
        "title": idea.title,
        "status": idea.status,
        "owner": idea.owner,
        "notes": idea.notes,
        "created_at": idea.created_at,
        "updated_at": idea.updated_at,
    }


def list_ideas(status: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as s:
        q = select(Idea).order_by(Idea.updated_at.desc())
        if status:
            q = q.where(Idea.status == status)
        return [_as_dict(i) for i in s.scalars(q)]


def get_idea(idea_id: int) -> dict[str, Any] | None:
    with session_scope() as s:
        idea = s.get(Idea, idea_id)
        if idea is None:
            return None
        out = _as_dict(idea)
        out["events"] = [
            {
                "from_status": e.from_status,
                "to_status": e.to_status,
                "note": e.note,
                "user_name": e.user_name,
                "ts": e.ts,
            }
            for e in s.scalars(
                select(IdeaEvent).where(IdeaEvent.idea_id == idea_id).order_by(IdeaEvent.ts)
            )
        ]
        out["comments"] = [
            {"id": c.id, "user_name": c.user_name, "body": c.body, "ts": c.ts}
            for c in s.scalars(
                select(IdeaComment).where(IdeaComment.idea_id == idea_id).order_by(IdeaComment.ts)
            )
        ]
        return out


def move_idea(idea_id: int, status: str, note: str | None = None, user: str = "me") -> None:
    if status not in IDEA_STATUSES:
        raise ValueError(f"status 必须是 {IDEA_STATUSES} 之一")
    with session_scope() as s:
        idea = s.get(Idea, idea_id)
        if idea is None:
            raise ValueError(f"点子 #{idea_id} 不存在")
        if idea.status == status:
            return
        s.add(
            IdeaEvent(
                idea_id=idea_id,
                from_status=idea.status,
                to_status=status,
                note=note,
                user_name=user,
            )
        )
        idea.status = status


def assign_idea(idea_id: int, owner: str) -> None:
    with session_scope() as s:
        idea = s.get(Idea, idea_id)
        if idea is None:
            raise ValueError(f"点子 #{idea_id} 不存在")
        idea.owner = owner or None


def add_comment(idea_id: int, body: str, user: str = "me") -> int:
    with session_scope() as s:
        if s.get(Idea, idea_id) is None:
            raise ValueError(f"点子 #{idea_id} 不存在")
        c = IdeaComment(idea_id=idea_id, body=body, user_name=user)
        s.add(c)
        s.flush()
        return c.id
