from datetime import UTC, date, datetime, timedelta

import pytest

from groundswell.configs import load_calendars, load_regions
from groundswell.processing.dedupe import (
    features,
    find_duplicates,
    fingerprint,
    hamming,
    simhash,
    to_signed,
    to_unsigned,
)
from groundswell.processing.keywords import extract_keywords
from groundswell.processing.lang import detect_language
from groundswell.processing.region import attribute, attribute_content
from groundswell.processing.seasonality import active_events, seasonal_factor
from groundswell.processing.timeutil import floor_hour, hour_range, local_day_window
from groundswell.processing.trend import first_breakout, score_series, trend_point


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("DeepSeek 发布新模型，推理成本下降九成", "zh"),
        ("ChatGPTの新機能が話題に", "ja"),
        ("삼성전자 신제품 발표", "ko"),
        ("ร้านกาแฟเปิดใหม่ในกรุงเทพ", "th"),
        ("Giá vàng hôm nay tăng mạnh", "vi"),
        ("Show HN: I built a tool to track your habits", "en"),
        ("Como ganhar dinheiro com inteligência artificial no Brasil", "pt"),
        ("Cómo mejorar tu negocio con inteligencia artificial", "es"),
        ("Die besten Tipps für die Steuererklärung", "de"),
    ],
)
def test_detect_language(text, lang):
    assert detect_language(text) == lang


def test_keywords_lowercase_and_stopwords():
    kws = extract_keywords("Show HN: Open-source AI agent for invoices — Acme Inc.", language="en")
    assert "ai" in kws and "AI" not in kws
    assert "inc" not in kws and "show" not in kws
    assert "open-source" in kws
    zh = extract_keywords("DeepSeek 发布开源推理模型，网友：成本太低了", language="zh")
    assert "deepseek" in zh
    assert "网友" not in zh


def test_simhash_near_duplicate():
    a = "OpenAI releases a new reasoning model with lower cost and better math performance"
    b = "OpenAI releases a new reasoning model with lower cost and better math performance!"
    c = "Local bakery wins national award for sourdough bread after ten years of trying"
    assert hamming(simhash(a), simhash(b)) <= 3
    assert hamming(simhash(a), simhash(c)) > 10
    assert to_unsigned(to_signed(simhash(a))) == simhash(a)
    assert -(1 << 63) <= to_signed((1 << 64) - 1) < (1 << 63)


def test_find_duplicates_exact_title_and_short_text():
    old = [fingerprint(1, "淄博烧烤火出圈")]
    new = [
        fingerprint(2, "淄博烧烤火出圈！"),  # 标点不同，规范化后相同
        fingerprint(3, "AI"),  # 短文本不做 simhash 近似匹配
        fingerprint(4, "AI"),
        fingerprint(5, "完全不同的另一条新闻内容"),
    ]
    dups = find_duplicates(old, new)
    assert dups == {2: 1, 4: 3}
    assert len(features("abc")) >= 1


def test_region_attribution_levels():
    regions = load_regions()
    kept = attribute(
        ingest_region="GC",
        ingest_country="CN",
        ingest_method="source",
        ingest_confidence=1.0,
        text="anything",
        language="en",
        regions=regions,
    )
    assert (kept.region, kept.method) == ("GC", "source")

    content = attribute_content("Why is everyone in Indonesia using this app?", "en", regions)
    assert content.region == "SEA" and content.country == "ID" and content.method == "content"

    weak = attribute_content("Como fazer pão", "pt", regions)
    assert weak.region == "GLOBAL"  # 仅语言证据不足以归属
    assert weak.weights.get("LATAM") == pytest.approx(0.4)

    fallback = attribute_content("A new JavaScript framework", "en", regions)
    assert fallback.region == "GLOBAL" and fallback.method == "fallback"


def test_trend_point_poisson_floor_and_min_sample():
    flat = trend_point([10] * 48, 10)
    assert flat.z == 0 and not flat.low_sample
    spike = trend_point([10] * 48, 30)
    assert spike.z > 5 and spike.growth > 0.5
    # 小基数：从 0 到 2 属于低样本，不给 z
    tiny = trend_point([0] * 48, 2, min_count=3)
    assert tiny.low_sample and tiny.z == 0
    # 季节性因子只压突发分量
    damped = trend_point([10] * 48, 30, seasonal=0.5)
    assert damped.score < spike.score and damped.heat == spike.heat


def test_score_series_first_breakout():
    series = [2.0] * 30 + [3.0, 5.0, 12.0, 20.0]
    points = score_series(series, window=24, min_count=3)
    assert first_breakout(points, z_threshold=2.0) == 31
    assert all(p.z == 0 for p in points[:30])


def test_seasonality_calendar():
    regions, calendars = load_regions(), load_calendars()
    singles_day = datetime(2026, 11, 11, 4, tzinfo=UTC)
    assert seasonal_factor("GC", singles_day, calendars, regions) < 1.0
    assert "双十一" in active_events("GC", singles_day, calendars, regions)
    assert seasonal_factor("GC", datetime(2026, 3, 18, 4, tzinfo=UTC), calendars, regions) == 1.0


def test_timeutil_windows():
    start, end = local_day_window("Asia/Shanghai", date(2026, 9, 27))
    assert start == datetime(2026, 9, 26, 16, tzinfo=UTC)
    assert end - start == timedelta(days=1)
    t = datetime(2026, 9, 27, 5, 42, tzinfo=UTC)
    assert floor_hour(t) == datetime(2026, 9, 27, 5, tzinfo=UTC)
    assert len(hour_range(t, t + timedelta(hours=3))) == 4
