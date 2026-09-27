import json

import httpx

from groundswell.collectors.base import Item, parse_int, strip_html
from groundswell.collectors.dailyhot import parse_dailyhot
from groundswell.collectors.google_trends import parse_trends_rss
from groundswell.collectors.registry import build_collector
from groundswell.collectors.rss import parse_feed
from groundswell.collectors.runner import build_rows, resolve_region
from groundswell.configs import SourceConfig, load_regions

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>第一条 &amp; 热点</title><link>https://a.example/1</link><guid>g1</guid>
<description><![CDATA[<p>正文 <b>一</b></p>]]></description><pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate></item>
<item><title>第二条</title><link>https://a.example/2</link></item>
</channel></rss>"""

TRENDS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>World Cup</title><ht:approx_traffic>200,000+</ht:approx_traffic>
<pubDate>Sun, 27 Sep 2026 08:00:00 -0700</pubDate>
<ht:news_item><ht:news_item_title>Headline</ht:news_item_title><ht:news_item_url>https://n.example</ht:news_item_url></ht:news_item>
</item></channel></rss>"""


def test_helpers():
    assert parse_int("200,000+") == 200000
    assert parse_int("2K+") == 2000
    assert parse_int("abc") is None
    assert strip_html("<p>a &amp; <b>b</b></p>") == "a & b"


def test_parse_feed_extracts_items_and_ranks():
    items = parse_feed(RSS, channel="/x")
    assert [i.title for i in items] == ["第一条 & 热点", "第二条"]
    assert items[0].external_id == "g1"
    assert items[0].text == "正文 一"
    assert items[0].published_at.year == 2026
    assert items[1].external_id == "https://a.example/2"
    assert [i.rank for i in items] == [1, 2]


def test_parse_trends_rss():
    [item] = parse_trends_rss(TRENDS, "US")
    assert item.country == "US"
    assert item.metrics["traffic"] == 200000
    assert item.external_id.startswith("US:world cup:2026-09-27")
    assert item.text == "Headline"


def test_parse_dailyhot():
    data = {
        "code": 200,
        "data": [
            {"id": 1, "title": "抖音话题", "hot": 123, "url": "u", "timestamp": 1790000000000}
        ],
    }
    [item] = parse_dailyhot(data, "douyin")
    assert item.metrics["heat"] == 123
    assert item.published_at.year >= 2026


def test_hackernews_collector_with_mock_transport(settings, mock_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "hn.algolia.com"
        hits = [
            {
                "objectID": "42",
                "title": "Show HN: a tool",
                "points": 99,
                "num_comments": 7,
                "created_at_i": 1790000000,
                "author": "pg",
            }
        ]
        return httpx.Response(200, content=json.dumps({"hits": hits}))

    cfg = SourceConfig(
        id="hn", kind="hackernews", name="HN", params={"tags": ["front_page", "show_hn"]}
    )
    items = build_collector(cfg, settings, mock_client(handler)).fetch()
    assert len(items) == 2
    assert items[0].metrics["points"] == 99
    assert items[0].rank == 1 and items[1].rank is None
    assert items[1].channel == "show_hn"


def test_region_resolution_priority():
    regions = load_regions()
    cfg = SourceConfig(
        id="reddit",
        kind="reddit",
        name="r",
        channels={"brasil": {"country": "BR"}, "europe": {"region": "EU"}},
    )
    assert resolve_region(cfg, Item(external_id="1", title="t", channel="brasil"), regions)[:2] == (
        "LATAM",
        "BR",
    )
    assert (
        resolve_region(cfg, Item(external_id="1", title="t", channel="europe"), regions)[0] == "EU"
    )
    assert resolve_region(cfg, Item(external_id="1", title="t", country="SG"), regions)[0] == "SEA"
    region, _, method, conf = resolve_region(cfg, Item(external_id="1", title="t"), regions)
    assert (region, method, conf) == ("GLOBAL", "source", 0.0)


def test_build_rows_dedupes_external_ids_and_hashes_authors():
    from datetime import UTC, datetime

    cfg = SourceConfig(id="s", kind="rss", name="s", region="GC", country="CN")
    items = [Item(external_id="a", title="x", author="alice"), Item(external_id="a", title="x2")]
    rows = build_rows(cfg, items, datetime.now(UTC), load_regions())
    assert len(rows) == 1
    assert rows[0]["author_hash"] and "alice" not in rows[0]["author_hash"]
    assert rows[0]["region"] == "GC"
