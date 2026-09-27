"""关键词订阅：话题在指定区域/类别内起飞（score ≥ min_score）且名称或关键词命中订阅词时推送。

每个 (订阅, 话题, 区域) 只提醒一次（subscription_hits 唯一约束）。
channel：default = 默认钉钉机器人；https://... = 自定义 webhook；none = 只记录不推送。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from groundswell.db.models import Subscription, SubscriptionHit
from groundswell.db.session import session_scope
from groundswell.processing.terms import matches_any
from groundswell.taxonomy import CATEGORIES


def add_subscription(
    name: str,
    keywords: list[str],
    regions: list[str] | None = None,
    categories: list[str] | None = None,
    min_score: float = 2.0,
    channel: str = "default",
) -> int:
    keywords = [k.strip() for k in keywords if k.strip()]
    if not keywords:
        raise ValueError("至少需要一个关键词")
    bad = [c for c in categories or [] if c not in CATEGORIES]
    if bad:
        raise ValueError(f"未知类别 {bad}")
    with session_scope() as s:
        sub = Subscription(
            name=name,
            keywords=keywords,
            regions=list(regions or []),
            categories=list(categories or []),
            min_score=min_score,
            channel=channel,
            enabled=True,
        )
        s.add(sub)
        s.flush()
        return sub.id


def list_subscriptions() -> list[dict[str, Any]]:
    with session_scope() as s:
        return [
            {
                "id": x.id,
                "name": x.name,
                "keywords": x.keywords,
                "regions": x.regions,
                "categories": x.categories,
                "min_score": x.min_score,
                "channel": x.channel,
                "enabled": x.enabled,
                "created_at": x.created_at,
            }
            for x in s.scalars(select(Subscription).order_by(Subscription.id))
        ]


def remove_subscription(subscription_id: int) -> None:
    with session_scope() as s:
        sub = s.get(Subscription, subscription_id)
        if sub:
            s.delete(sub)


def check_subscriptions(hours: int = 3, push: bool = True) -> int:
    """返回新命中数。"""
    new_hits: list[tuple[Subscription, dict]] = []
    with session_scope() as s:
        subs = list(s.scalars(select(Subscription).where(Subscription.enabled.is_(True))))
        if not subs:
            return 0
        rows = s.execute(
            text(
                """
                SELECT st.topic_id, st.region, max(st.score) AS score, t.name, t.keywords, t.category
                FROM topic_hourly_stats st JOIN topics t ON t.id = st.topic_id AND t.status = 'active'
                WHERE st.hour >= now() - make_interval(hours => :h) AND st.z IS NOT NULL
                GROUP BY st.topic_id, st.region, t.name, t.keywords, t.category
                """
            ),
            {"h": hours},
        ).all()
        for sub in subs:
            for r in rows:
                if r.score is None or r.score < sub.min_score:
                    continue
                if sub.regions and r.region not in sub.regions:
                    continue
                if sub.categories and r.category not in sub.categories:
                    continue
                if not matches_any(
                    " ".join([r.name or "", " ".join(r.keywords or [])]), sub.keywords
                ):
                    continue
                stmt = (
                    insert(SubscriptionHit)
                    .values(
                        subscription_id=sub.id,
                        topic_id=r.topic_id,
                        region=r.region,
                        score=float(r.score),
                    )
                    .on_conflict_do_nothing()
                    .returning(SubscriptionHit.id)
                )
                if s.execute(stmt).first():
                    new_hits.append((sub, dict(r._mapping)))
        s.expunge_all()
    if push and new_hits:
        _push(new_hits)
    return len(new_hits)


def _push(hits: list[tuple[Subscription, dict]]) -> None:
    from groundswell.reporting.dingtalk import send_markdown

    by_channel: dict[str, list[str]] = {}
    for sub, r in hits:
        if sub.channel == "none":
            continue
        line = f"- 【{sub.name}】[{r['region']}] #{r['topic_id']} {r['name']}（score {r['score']:.2f}）"
        by_channel.setdefault(sub.channel, []).append(line)
    for channel, lines in by_channel.items():
        body = "#### 观潮订阅提醒\n\n" + "\n".join(lines[:40])
        webhook = None if channel == "default" else channel
        sent = send_markdown("观潮订阅提醒", body, webhook=webhook)
        logger.info(f"订阅推送 {len(lines)} 条 → {channel}（成功 {sent}）")
