"""FastAPI：健康检查 + 前端（Next.js / Streamlit）与外部集成使用的只读/轻写接口。

鉴权：配置 GS_API_TOKEN 后，/api/* 需要 `Authorization: Bearer <token>`；/healthz 永远开放。
多租户预留：所有查询都经过 queries 层，未来按 workspace_id 过滤只需改一处（ADR-010）。
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell import __version__, queries
from groundswell.config import get_settings
from groundswell.configs import load_regions
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import now_utc
from groundswell.taxonomy import (
    BUSINESS_CATEGORIES,
    CATEGORY_LABELS,
    FEEDBACK_LABELS,
    IDEA_STATUSES,
)


def get_session() -> Iterator[Session]:
    with session_scope() as s:
        yield s


class FeedbackIn(BaseModel):
    label: str = Field(pattern="^(good|meh|bad)$")
    note: str | None = None
    user: str = "me"


class IdeaIn(BaseModel):
    opportunity_id: int
    user: str = "me"


class IdeaPatch(BaseModel):
    status: str | None = None
    owner: str | None = None
    note: str | None = None
    user: str = "me"


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=5000)
    user: str = "me"


class SubscriptionIn(BaseModel):
    name: str
    keywords: list[str] = Field(min_length=1)
    regions: list[str] = []
    categories: list[str] = []
    min_score: float = 2.0
    channel: str = "default"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Groundswell 观潮", version=__version__)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
    )

    @app.middleware("http")
    async def auth(request: Request, call_next):
        token = settings.api_token
        if token and request.url.path.startswith("/api/") and request.method != "OPTIONS":
            header = request.headers.get("authorization", "")
            given = header.removeprefix("Bearer ").strip()
            if not hmac.compare_digest(given, token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ------------------------------------------------------------ 基础

    @app.get("/healthz")
    def healthz(s: Session = Depends(get_session)) -> dict[str, Any]:
        s.execute(text("SELECT 1"))
        last = s.execute(text("SELECT max(fetched_at) FROM raw_items")).scalar()
        return {"ok": True, "version": __version__, "last_item_at": last}

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        regions = load_regions()
        return {
            "regions": [
                {"code": c, "name": r.name, "timezone": r.timezone}
                for c, r in regions.regions.items()
            ],
            "report_regions": settings.report_regions,
            "categories": [{"code": c, "label": CATEGORY_LABELS[c]} for c in CATEGORY_LABELS if c],
            "business_categories": list(BUSINESS_CATEGORIES),
            "idea_statuses": list(IDEA_STATUSES),
        }

    # ------------------------------------------------------------ 话题

    @app.get("/api/rankings")
    def rankings(
        region: str = "GLOBAL",
        sort: str = Query("heat", pattern="^(heat|growth|mentions)$"),
        hours: int = Query(24, ge=1, le=24 * 30),
        category: str | None = None,
        business_only: bool = False,
        limit: int = Query(20, ge=1, le=200),
        s: Session = Depends(get_session),
    ) -> dict[str, Any]:
        end = now_utc()
        start = end - timedelta(hours=hours)
        cats = (category,) if category else (BUSINESS_CATEGORIES if business_only else None)
        thr = settings.trend_z_threshold if sort == "growth" else None
        topics = queries.topic_ranking(
            s, region, start, end, sort=sort, limit=limit, categories=cats, z_threshold=thr
        )
        payload: dict[str, Any] = {"region": region, "sort": sort, "topics": topics}
        if not topics:
            payload["items"] = queries.item_heat(s, region, start, end, limit=limit)
            payload["keywords"] = queries.keyword_growth(
                s, region, start, end, settings.trend_z_threshold, limit=limit
            )
        return payload

    @app.get("/api/topics/{topic_id}")
    def topic(topic_id: int, hours: int = 168, s: Session = Depends(get_session)) -> dict[str, Any]:
        detail = queries.topic_detail(s, topic_id)
        if not detail:
            raise HTTPException(404, "topic not found")
        detail["series"] = queries.topic_series(s, topic_id, hours=hours)
        detail["similar"] = queries.similar_topics(s, topic_id)
        return detail

    @app.get("/api/search")
    def search(q: str = Query(min_length=1), limit: int = 20, s: Session = Depends(get_session)):
        return queries._rows(
            s,
            """
            SELECT id, name, category, item_count, first_seen, last_seen, status FROM topics
            WHERE status <> 'merged' AND (name ILIKE :q OR :kw = ANY(keywords))
            ORDER BY last_seen DESC LIMIT :limit
            """,
            q=f"%{q}%",
            kw=q.lower(),
            limit=limit,
        )

    @app.get("/api/matrix")
    def matrix(days: int = 7, limit: int = 20, s: Session = Depends(get_session)):
        return queries.region_matrix(
            s, [r for r in load_regions().codes if r != "GLOBAL"], days, limit
        )

    @app.get("/api/spread")
    def spread(days: int = 7, limit: int = 20, s: Session = Depends(get_session)):
        return queries.recent_spread(s, days=days, limit=limit)

    # ------------------------------------------------------------ 商机

    @app.get("/api/opportunities")
    def opportunities(
        region: str | None = None, days: int = 7, limit: int = 50, s: Session = Depends(get_session)
    ):
        return queries.opportunities(s, region=region, days=days, limit=limit)

    @app.get("/api/opportunities/{opp_id}")
    def opportunity(opp_id: int, s: Session = Depends(get_session)) -> dict[str, Any]:
        rows = queries._rows(
            s,
            """
            SELECT o.*, t.name AS topic_name, t.category FROM opportunities o
            JOIN topics t ON t.id = o.topic_id WHERE o.id = :id
            """,
            id=opp_id,
        )
        if not rows:
            raise HTTPException(404, "opportunity not found")
        row = rows[0]
        row["evidence"] = queries.evidence_for(s, opp_id)
        return row

    @app.post("/api/opportunities/{opp_id}/feedback")
    def feedback(opp_id: int, body: FeedbackIn) -> dict[str, Any]:
        from groundswell.ideas import record_feedback

        if body.label not in FEEDBACK_LABELS:
            raise HTTPException(422, "bad label")
        idea_id = record_feedback(opp_id, body.label, note=body.note, user=body.user)
        return {"ok": True, "idea_id": idea_id}

    # ------------------------------------------------------------ 点子库

    @app.get("/api/ideas")
    def ideas(status: str | None = None):
        from groundswell.ideas import list_ideas

        return list_ideas(status)

    @app.get("/api/ideas/{idea_id}")
    def idea(idea_id: int):
        from groundswell.ideas import get_idea

        found = get_idea(idea_id)
        if not found:
            raise HTTPException(404, "idea not found")
        return found

    @app.post("/api/ideas")
    def idea_add(body: IdeaIn):
        from groundswell.ideas import add_from_opportunity

        return {"id": add_from_opportunity(body.opportunity_id, user=body.user)}

    @app.patch("/api/ideas/{idea_id}")
    def idea_patch(idea_id: int, body: IdeaPatch):
        from groundswell.ideas import assign_idea, move_idea

        if body.status:
            if body.status not in IDEA_STATUSES:
                raise HTTPException(422, "bad status")
            move_idea(idea_id, body.status, note=body.note, user=body.user)
        if body.owner is not None:
            assign_idea(idea_id, body.owner)
        return {"ok": True}

    @app.post("/api/ideas/{idea_id}/comments")
    def idea_comment(idea_id: int, body: CommentIn):
        from groundswell.ideas import add_comment

        return {"id": add_comment(idea_id, body.body, user=body.user)}

    # ------------------------------------------------------------ 订阅

    @app.get("/api/subscriptions")
    def subscriptions():
        from groundswell.subscriptions import list_subscriptions

        return list_subscriptions()

    @app.post("/api/subscriptions")
    def subscription_add(body: SubscriptionIn):
        from groundswell.subscriptions import add_subscription

        sid = add_subscription(
            body.name, body.keywords, body.regions, body.categories, body.min_score, body.channel
        )
        return {"id": sid}

    @app.delete("/api/subscriptions/{sub_id}")
    def subscription_rm(sub_id: int):
        from groundswell.subscriptions import remove_subscription

        remove_subscription(sub_id)
        return {"ok": True}

    # ------------------------------------------------------------ 运维与报告

    @app.get("/api/health/sources")
    def health_sources(hours: int = 24, s: Session = Depends(get_session)):
        return queries.source_health(s, hours=hours)

    @app.get("/api/reports")
    def reports(
        kind: str = Query("daily", pattern="^(daily|weekly|monthly|brief|backtest|reviews)$"),
    ):
        base = Path(settings.reports_dir) / kind
        files = sorted(base.glob("*.md"), reverse=True)[:100] if base.exists() else []
        return [{"name": f.stem, "size": f.stat().st_size} for f in files]

    @app.get("/api/reports/{kind}/{name}", response_class=PlainTextResponse)
    def report(kind: str, name: str) -> str:
        if not name.replace("-", "").replace("_", "").isalnum() or kind not in (
            "daily",
            "weekly",
            "monthly",
            "brief",
            "backtest",
            "reviews",
        ):
            raise HTTPException(400, "bad name")
        path = Path(settings.reports_dir) / kind / f"{name}.md"
        if not path.exists():
            raise HTTPException(404, "report not found")
        return path.read_text(encoding="utf-8")

    return app
