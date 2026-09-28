from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, text

from groundswell import llm
from groundswell.config import Settings
from groundswell.db.models import Evidence, Idea, LlmUsage, Opportunity
from groundswell.db.session import session_scope
from groundswell.opportunity import funnel
from groundswell.opportunity.schemas import DIMENSIONS, OpportunityAnalysis, ScreenResult
from groundswell.processing.timeutil import floor_hour, now_utc

pytestmark = pytest.mark.db


def seed_topic(category: str = "demand") -> tuple[int, list[int]]:
    hour = floor_hour(now_utc()) - timedelta(hours=1)
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO sources (id, kind, name, default_region, config, enabled, consecutive_failures, "
                "last_item_count) VALUES ('s1', 'rss', 's1', 'GC', '{}', true, 0, 0)"
            )
        )
        ids = []
        for i, title in enumerate(
            ["有没有好用的发票报销工具", "报销流程太麻烦了", "求推荐自动对账软件"]
        ):
            ids.append(
                s.execute(
                    text(
                        "INSERT INTO raw_items (source_id, external_id, title, fetched_at, last_seen_at, metrics, "
                        "region, language) VALUES ('s1', :e, :t, :ts, :ts, '{}', 'GC', 'zh') RETURNING id"
                    ),
                    {"e": f"e{i}", "t": title, "ts": hour},
                ).scalar_one()
            )
        tid = s.execute(
            text(
                "INSERT INTO topics (name, category, keywords, item_count, source_count, active_regions, "
                "slow_variables, status, first_seen, last_seen) VALUES ('报销对账工具需求', :c, "
                "'{报销,对账}', 3, 2, '{GC}', '{}', 'active', :ts, :ts) RETURNING id"
            ),
            {"c": category, "ts": hour},
        ).scalar_one()
        for i in ids:
            s.execute(
                text(
                    "INSERT INTO topic_items (topic_id, item_id, similarity) VALUES (:t, :i, 0.9)"
                ),
                {"t": tid, "i": i},
            )
        s.execute(
            text(
                "INSERT INTO topic_hourly_stats (topic_id, region, hour, mentions, new_items, engagement, "
                "sources_count, z, growth, score) VALUES (:t, 'GC', :h, 10, 3, 5, 2, 3.5, 1.2, 2.4)"
            ),
            {"t": tid, "h": hour},
        )
    return tid, ids


@pytest.fixture
def fake_llm(monkeypatch):
    calls = {"screen": 0, "deep": 0}
    state = {"evidence": None, "budget_on_deep": False}

    def complete_json(prompt, *, system, schema=None, tier="cheap", stage, **_):
        calls[stage] = calls.get(stage, 0) + 1
        if schema is ScreenResult:
            import re

            ids = [int(x) for x in re.findall(r"\[id=(\d+)\]", prompt)]
            return ScreenResult.model_validate(
                {"topics": [{"id": i, "keep": True, "score": 8} for i in ids]}
            )
        if schema is OpportunityAnalysis:
            if state["budget_on_deep"]:
                raise llm.BudgetExceeded("budget")
            return OpportunityAnalysis.model_validate(
                {
                    "summary": "给中小企业财务的自动报销对账工具",
                    "scores": dict.fromkeys(DIMENSIONS, 7),
                    "tags": ["unmet_demand", "schlep"],
                    "principles_applied": ["pg_make_something_people_want"],
                    "why_now": "z=3.5",
                    "distribution_path": "企业微信 / 钉钉应用市场",
                    "product_shapes": ["钉钉报销小程序"],
                    "counter_arguments": ["大厂已有"],
                    "known_players": ["合思"],
                    "durability": "structural",
                    "evidence": state["evidence"],
                }
            )
        raise AssertionError(schema)

    monkeypatch.setattr(llm, "available", lambda tier="cheap", settings=None: True)
    monkeypatch.setattr(llm, "model_for", lambda tier, settings=None: f"fake/{tier}")
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return calls, state


def test_funnel_llm_path_enforces_evidence(db, fake_llm):
    calls, state = fake_llm
    tid, ids = seed_topic()
    state["evidence"] = [
        {"item_id": ids[0], "kind": "demand", "quote": "有没有好用的发票报销工具"},
        {"item_id": ids[1], "kind": "pain", "quote": "报销流程太麻烦了"},
        {"item_id": 99999999, "kind": "other", "quote": "编造的证据"},
    ]
    stats = funnel.run_funnel(regions=["GC"], deep_limit=5)
    assert stats["regions"]["GC"]["llm"] == 1
    assert calls["screen"] == 1 and calls["deep"] == 1
    with session_scope() as s:
        opp = s.scalar(select(Opportunity).where(Opportunity.topic_id == tid))
        assert opp.stage == "deep" and opp.model == "fake/strong"
        assert opp.total_score == pytest.approx(7 + min(1.0, 0.1 * (1.5 + 1.2)))
        n_ev = s.scalar(
            select(func.count()).select_from(Evidence).where(Evidence.opportunity_id == opp.id)
        )
        assert n_ev == 2  # 编造的 item_id 被丢弃
    # 3 天内不重复分析
    again = funnel.run_funnel(regions=["GC"], deep_limit=5)
    assert again["regions"]["GC"]["candidates"] == 0


def test_funnel_rejects_without_evidence(db, fake_llm):
    _, state = fake_llm
    tid, ids = seed_topic()
    state["evidence"] = [{"item_id": ids[0], "kind": "demand", "quote": "只有一条"}]
    funnel.run_funnel(regions=["GC"], deep_limit=5)
    with session_scope() as s:
        opp = s.scalar(select(Opportunity).where(Opportunity.topic_id == tid))
        assert opp.stage == "rejected"


def test_budget_exceeded_falls_back_to_heuristic(db, fake_llm):
    _, state = fake_llm
    tid, _ = seed_topic()
    state["budget_on_deep"] = True
    stats = funnel.run_funnel(regions=["GC"], deep_limit=5)
    assert stats["regions"]["GC"]["heuristic"] == 1
    with session_scope() as s:
        opp = s.scalar(select(Opportunity).where(Opportunity.topic_id == tid))
        assert opp.model == "heuristic" and opp.stage == "deep"
        assert "unmet_demand" in opp.tags  # 启发式也能命中方法论信号


def test_heuristic_only_when_no_llm(db):
    tid, _ = seed_topic(category="tech")
    stats = funnel.run_funnel(regions=["GC"])
    assert stats["llm"] is False
    assert stats["regions"]["GC"]["heuristic"] == 1


def test_llm_budget_guard(db):
    settings = Settings(
        _env_file=None, llm_cheap_model="x/y", llm_daily_budget_cny=1.0, usd_to_cny=7.0
    )
    with session_scope() as s:
        s.add(
            LlmUsage(model="x/y", stage="deep", prompt_tokens=1, completion_tokens=1, cost_usd=0.2)
        )
    assert llm.spent_today_cny(settings) == pytest.approx(1.4)
    with pytest.raises(llm.BudgetExceeded):
        llm.complete([{"role": "user", "content": "hi"}], stage="t", settings=settings)


def test_feedback_creates_idea_and_lifecycle(db, fake_llm):
    from groundswell.ideas import add_comment, get_idea, list_ideas, move_idea, record_feedback

    _, state = fake_llm
    tid, ids = seed_topic()
    state["evidence"] = [
        {"item_id": ids[0], "kind": "demand", "quote": "a"},
        {"item_id": ids[1], "kind": "pain", "quote": "b"},
    ]
    funnel.run_funnel(regions=["GC"], deep_limit=5)
    with session_scope() as s:
        opp_id = s.scalar(select(Opportunity.id).where(Opportunity.topic_id == tid))
    idea_id = record_feedback(opp_id, "good", note="值得做")
    assert idea_id and record_feedback(opp_id, "good") == idea_id  # 幂等
    move_idea(idea_id, "watching", note="开始观察")
    add_comment(idea_id, "找两个财务朋友访谈", user="alice")
    detail = get_idea(idea_id)
    assert detail["status"] == "watching"
    assert [e["to_status"] for e in detail["events"]] == ["candidate", "watching"]
    assert detail["comments"][0]["user_name"] == "alice"
    assert "钉钉报销小程序" in detail["title"]
    assert [i["id"] for i in list_ideas("watching")] == [idea_id]
    with pytest.raises(ValueError):
        record_feedback(opp_id, "great")
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Idea)) == 1


def test_validators_parse_responses(settings):
    from groundswell.opportunity import validators as v

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.github.com" in url:
            return httpx.Response(
                200,
                json={"total_count": 12, "items": [{"full_name": "a/b", "stargazers_count": 99}]},
            )
        if "itunes.apple.com" in url:
            return httpx.Response(
                200, json={"results": [{"trackName": "X", "userRatingCount": 5}] * 10}
            )
        if "search_by_date" in url:
            return httpx.Response(
                200, json={"hits": [{"objectID": "1", "title": "Ask HN: Who is hiring? (May)"}]}
            )
        if "hn.algolia.com" in url:
            return httpx.Response(
                200,
                json={
                    "nbHits": 3,
                    "hits": [{"title": "X raises $5M", "comment_text": "Acme | SF"}],
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    v._HIRING_CACHE.update(ts=0.0, ids=[])
    sig, payload = v.validate_github("invoice ai", client=client, settings=settings)
    assert payload["count"] == 12 and sig > 0
    sig, payload = v.validate_appstore("invoice", country="CN", client=client)
    assert payload["count"] == 10 and sig == pytest.approx(0.2)
    sig, payload = v.validate_jobs("invoice", client=client)
    assert payload["count"] == 3 and payload["examples"] == ["Acme"]
    sig, payload = v.validate_funding("invoice", client=client)
    assert payload["count"] == 9
