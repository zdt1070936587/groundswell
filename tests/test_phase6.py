import httpx
import pytest
from sqlalchemy import select, text

from groundswell.config import Settings
from groundswell.db.models import Opportunity
from groundswell.db.session import session_scope
from groundswell.reviews import heuristic_pains, mine_app_reviews, parse_reviews


def entry(i, rating, title, content, version="2.0"):
    return {
        "id": {"label": str(i)},
        "im:rating": {"label": str(rating)},
        "title": {"label": title},
        "content": {"label": content},
        "im:version": {"label": version},
        "author": {"name": {"label": f"u{i}"}},
    }


FEED = {
    "feed": {
        "entry": [
            {"im:name": {"label": "应用信息（旧格式首条）"}},
            entry(1, 1, "Crashes all the time", "The app crashes every time I open it"),
            entry(2, 2, "Too expensive", "The subscription price doubled, want a refund"),
            entry(3, 1, "闪退", "更新后一直闪退，打不开"),
            entry(4, 5, "Love it", "Great app"),
            entry(5, 2, "Meh", "Colors are weird"),
        ]
    }
}


def test_parse_and_cluster_reviews():
    reviews = parse_reviews(FEED)
    assert [r.rating for r in reviews] == [1, 2, 1, 5, 2]
    pains = heuristic_pains([r for r in reviews if r.rating <= 2])
    by_name = {p.name: p for p in pains}
    assert by_name["闪退 / Bug"].count == 2
    assert by_name["付费 / 订阅 / 价格"].count == 1
    assert by_name["其他"].count == 1
    assert pains[0].name == "闪退 / Bug"


def test_mine_app_reviews_writes_report(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lookup" in url:
            return httpx.Response(200, json={"results": [{"trackName": "DemoApp"}]})
        if "page=1/" in url:
            return httpx.Response(200, json=FEED)
        return httpx.Response(200, json={"feed": {}})

    settings = Settings(_env_file=None, reports_dir=tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    path, md = mine_app_reviews("123", "us", pages=3, settings=settings, client=client)
    assert path.parent == tmp_path / "reviews" and path.name.startswith("123-us-")
    assert "差评挖掘 · DemoApp" in md and "低星 4 条" in md
    assert "闪退 / Bug | 2 | 50%" in md


@pytest.mark.db
def test_policy_validator_uses_collected_documents(db):
    from groundswell.opportunity.validators import validate_policy

    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO sources (id, kind, name, default_region, config, enabled, consecutive_failures, "
                "last_item_count) VALUES ('gov_policy', 'rsshub', 'gov', 'GC', '{}', true, 0, 0)"
            )
        )
        for i, title in enumerate(["关于推进电子发票报销入账归档的通知", "关于体育赛事的意见"]):
            s.execute(
                text(
                    "INSERT INTO raw_items (source_id, external_id, title, url, fetched_at, last_seen_at, metrics) "
                    "VALUES ('gov_policy', :e, :t, 'https://gov.cn/x', now(), now(), '{}')"
                ),
                {"e": f"g{i}", "t": title},
            )
    signal, payload = validate_policy("报销")
    assert payload["count"] == 1 and signal > 0
    assert payload["examples"][0]["title"].startswith("关于推进电子发票")


@pytest.mark.db
def test_brief_from_heuristic_opportunity(db, tmp_path, monkeypatch):
    from tests.test_opportunity_db import seed_topic

    from groundswell.opportunity import funnel
    from groundswell.reporting import brief

    tid, _ = seed_topic()
    funnel.run_funnel(regions=["GC"])
    with session_scope() as s:
        opp_id = s.scalar(select(Opportunity.id).where(Opportunity.topic_id == tid))
    monkeypatch.setattr(
        brief, "get_settings", lambda: Settings(_env_file=None, reports_dir=tmp_path)
    )
    path, md = brief.build_brief(opp_id)
    assert path == tmp_path / "brief" / f"opp-{opp_id}.md"
    for heading in (
        "为什么是现在",
        "信号与证据",
        "区域上下文",
        "一页纸商业计划（草稿，模板生成）",
        "四周验证计划",
    ):
        assert heading in md
    assert "报销" in md
    with pytest.raises(ValueError):
        brief.build_brief(99999999)
