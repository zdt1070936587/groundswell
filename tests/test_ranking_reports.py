from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy import text

from groundswell import taskqueue
from groundswell.db.models import Feedback, Opportunity
from groundswell.db.session import session_scope
from groundswell.opportunity import ranker
from groundswell.opportunity.schemas import DIMENSIONS
from groundswell.processing.timeutil import now_utc
from groundswell.reporting.periodic import build_periodic, period_window

TZ = ZoneInfo("Asia/Shanghai")


def test_period_window():
    since, until, label = period_window("weekly", date(2026, 9, 30), TZ)
    assert label == "2026-W39"
    assert since == datetime(2026, 9, 21, tzinfo=TZ) and until == datetime(2026, 9, 28, tzinfo=TZ)
    since, until, label = period_window("monthly", date(2026, 9, 1), TZ)
    assert label == "2026-08"
    assert since == datetime(2026, 8, 1, tzinfo=TZ) and until == datetime(2026, 9, 1, tzinfo=TZ)


def test_logistic_learns_separable_signal():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(60, 3))
    y = (x[:, 1] > 0).astype(float)
    model = ranker.fit_logistic(x, y)
    assert ranker.auc(y, ranker.predict_logistic(model, x)) > 0.95
    assert abs(model["w"][1]) > abs(model["w"][0])


class FakeRedis:
    def __init__(self):
        self.items: list[str] = []
        self.members: set[str] = set()

    def sadd(self, key, value):
        if value in self.members:
            return 0
        self.members.add(value)
        return 1

    def srem(self, key, value):
        self.members.discard(value)

    def lpush(self, key, value):
        self.items.insert(0, value)

    def brpop(self, key, timeout=0):
        return (key, self.items.pop()) if self.items else None


def test_taskqueue_dedupes_until_done(monkeypatch):
    ran = []
    monkeypatch.setattr(taskqueue, "resolve", lambda name: lambda: ran.append(name))
    r = FakeRedis()
    assert taskqueue.enqueue("daily", r) is True
    assert taskqueue.enqueue("daily", r) is False
    assert taskqueue.enqueue("collect:hackernews", r) is True
    assert taskqueue.run_one(r) == "daily"
    assert taskqueue.enqueue("daily", r) is True  # 执行完成后可再次投递
    assert taskqueue.run_one(r) == "collect:hackernews"
    assert ran == ["daily", "collect:hackernews"]


# ---------------------------------------------------------------- 数据库


def seed_opportunities(n: int = 24, created_at: datetime | None = None) -> list[int]:
    created_at = created_at or now_utc()
    with session_scope() as s:
        tid = s.execute(
            text(
                "INSERT INTO topics (name, category, keywords, item_count, source_count, active_regions, "
                "slow_variables, status, first_seen, last_seen) VALUES ('t', 'demand', '{}', 1, 1, '{}', "
                "'{}', 'active', now(), now()) RETURNING id"
            )
        ).scalar_one()
        ids = []
        for i in range(n):
            good = i % 2 == 0
            score = 7.5 if good else 4.0
            opp = Opportunity(
                topic_id=tid,
                region="GC",
                input_hash=f"h{i}",
                prompt_version="p",
                methodology_version="m",
                model="fake",
                stage="deep",
                summary=f"商机 {i}",
                scores=dict.fromkeys(DIMENSIONS, score),
                total_score=score,
                tags=["unmet_demand"] if good else [],
                created_at=created_at,
            )
            s.add(opp)
            s.flush()
            s.add(Feedback(opportunity_id=opp.id, label="good" if good else "bad", ts=created_at))
            ids.append(opp.id)
    return ids


@pytest.mark.db
def test_ranker_train_and_apply(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ranker, "_model_path", lambda: tmp_path / "ranker.json")
    assert ranker.train()["status"] == "no_feedback"
    ids = seed_opportunities()
    out = ranker.train()
    assert out["status"] == "trained" and out["kind"] == "logistic"
    assert out["train_auc"] == 1.0
    assert ranker.apply(days=7) == len(ids)
    with session_scope() as s:
        scores = dict(s.execute(text("SELECT id, rank_score FROM opportunities ORDER BY id")).all())
    good = [scores[i] for i in ids[::2]]
    bad = [scores[i] for i in ids[1::2]]
    assert min(good) > max(bad)


@pytest.mark.db
def test_ranker_needs_both_labels(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ranker, "_model_path", lambda: tmp_path / "ranker.json")
    seed_opportunities(n=1)
    assert ranker.train()["status"] == "insufficient"
    assert ranker.apply() == 0


@pytest.mark.db
def test_weekly_report(db):
    today = now_utc().astimezone(TZ).date()
    since, _, label = period_window("weekly", today, TZ)
    seed_opportunities(n=4, created_at=since + timedelta(days=1))
    got_label, md = build_periodic_session("weekly", today)
    assert got_label == label
    assert "本期商机" in md and "有价值比例 **50%**" in md


def build_periodic_session(kind, today):
    with session_scope() as s:
        return build_periodic(s, kind, today)
