import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text

from groundswell.backtest import evaluate_case, run_backtest, summarize
from groundswell.configs import GoldenCase
from groundswell.db.models import Opportunity, Topic
from groundswell.db.session import session_scope
from groundswell.opportunity.spread import RegionStat, build_path, find_gaps, run_spread
from groundswell.processing.timeutil import floor_hour, now_utc

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def stat(region, hours_seen, breakout_h=None, z=None, mentions=5.0):
    return RegionStat(
        region,
        T0 + timedelta(hours=hours_seen),
        None if breakout_h is None else T0 + timedelta(hours=breakout_h),
        z,
        mentions,
    )


def test_build_path_orders_by_breakout_then_first_seen():
    stats = [
        stat("NA", 0, breakout_h=10, z=3.0),
        stat("GC", 5, breakout_h=6, z=4.0),
        stat("SEA", 1, mentions=4.0),
        stat("EU", 2, mentions=1.0),  # 讨论量不足，不进路径
        stat("GLOBAL", 0, breakout_h=1, z=5.0),
    ]
    path = build_path(stats, min_mentions=3.0)
    assert [p["region"] for p in path] == ["GC", "NA", "SEA"]
    assert path[0]["first_breakout"].startswith("2026-09-01T06")
    assert path[2]["first_breakout"] is None


def test_find_gaps_requires_two_breakouts():
    one = [stat("GC", 0, breakout_h=1, z=3.0)]
    assert find_gaps(one, ["GC", "NA"]) == []
    two = one + [stat("NA", 2, breakout_h=3, z=2.5), stat("SEA", 1, mentions=2.0)]
    gaps = find_gaps(two, ["GLOBAL", "GC", "NA", "SEA", "EU"])
    assert gaps == [("SEA", "lagging"), ("EU", "absent")]


CASE = GoldenCase(
    name="x", keywords=["widgetx"], region="GLOBAL", breakout=date(2024, 3, 1), approx=False
)


def series(spike_day: int, days: int = 140, base: float = 1.0, spike: float = 20.0):
    return [spike if i >= spike_day else base for i in range(days)]


def test_evaluate_case_hit_late_miss_no_data():
    start = CASE.breakout - timedelta(days=120)
    hit = evaluate_case(series(110), start, CASE, z_threshold=2.0)
    assert hit.status == "hit" and hit.lead_days == 10
    late = evaluate_case(series(125), start, CASE, z_threshold=2.0)
    assert late.status == "late" and late.lead_days == -5
    approx = CASE.model_copy(update={"approx": True})
    assert evaluate_case(series(125), start, approx, z_threshold=2.0).status == "hit"
    assert evaluate_case([2.0] * 140, start, CASE, z_threshold=2.0).status == "miss"
    assert evaluate_case([0.0] * 140, start, CASE, z_threshold=2.0).status == "no_data"


def test_evaluate_case_ignores_spikes_long_before_breakout():
    start = CASE.breakout - timedelta(days=120)
    counts = series(110)
    counts[30] = 30.0  # 起飞前 90 天的同名词噪声
    r = evaluate_case(counts, start, CASE, z_threshold=2.0)
    assert r.signal_day == start + timedelta(days=110)
    s = summarize([r, evaluate_case([0.0] * 140, start, CASE, z_threshold=2.0)])
    assert s["with_data"] == 1 and s["hit_rate"] == 1.0 and s["avg_lead_days"] == 10


# ---------------------------------------------------------------- 数据库


def seed_spread_topic() -> int:
    base = floor_hour(now_utc()) - timedelta(hours=30)
    with session_scope() as s:
        s.execute(
            text(
                "INSERT INTO sources (id, kind, name, default_region, config, enabled, consecutive_failures, "
                "last_item_count) VALUES ('s1', 'rss', 's1', 'GC', '{}', true, 0, 0)"
            )
        )
        tid = s.execute(
            text(
                "INSERT INTO topics (name, category, keywords, item_count, source_count, active_regions, "
                "slow_variables, status, first_seen, last_seen) VALUES ('AI 记账', 'demand', '{记账}', 3, 1, "
                "'{GC,NA}', '{}', 'active', :ts, :ts) RETURNING id"
            ),
            {"ts": base},
        ).scalar_one()
        for i, (title, region) in enumerate(
            [
                ("AI 记账太好用了", "GC"),
                ("AI bookkeeping app is blowing up", "NA"),
                ("求推荐记账工具", "GC"),
            ]
        ):
            item = s.execute(
                text(
                    "INSERT INTO raw_items (source_id, external_id, title, fetched_at, last_seen_at, metrics, "
                    "region) VALUES ('s1', :e, :t, :ts, :ts, '{}', :r) RETURNING id"
                ),
                {"e": f"e{i}", "t": title, "ts": base, "r": region},
            ).scalar_one()
            s.execute(
                text(
                    "INSERT INTO topic_items (topic_id, item_id, similarity) VALUES (:t, :i, 0.9)"
                ),
                {"t": tid, "i": item},
            )
        for region, offset, mentions, z in [
            ("GC", 0, 2, None),
            ("GC", 2, 12, 3.5),
            ("NA", 10, 9, 2.6),
            ("SEA", 12, 2, 0.5),
            ("GLOBAL", 2, 20, 4.0),
        ]:
            s.execute(
                text(
                    "INSERT INTO topic_hourly_stats (topic_id, region, hour, mentions, new_items, engagement, "
                    "sources_count, z) VALUES (:t, :r, :h, :m, 1, 1, 1, :z)"
                ),
                {"t": tid, "r": region, "h": base + timedelta(hours=offset), "m": mentions, "z": z},
            )
    return tid


@pytest.mark.db
def test_run_spread_paths_and_gap_analysis(db):
    tid = seed_spread_topic()
    out = run_spread(days=3, analyze_gaps=True)
    assert out["multi_region"] == 1
    with session_scope() as s:
        topic = s.get(Topic, tid)
        assert [p["region"] for p in topic.spread_path] == ["GC", "NA"]
        assert topic.origin_region == "GC"
        gaps = s.scalars(select(Opportunity).where(Opportunity.is_gap)).all()
    by_region = {o.region: o for o in gaps}
    assert set(by_region) == {"SEA", "LATAM", "EU"}
    assert by_region["SEA"].gap_reason == "lagging" and by_region["EU"].gap_reason == "absent"
    assert "GC、NA" in by_region["EU"].summary
    assert out["analyzed"]["heuristic"] == 3
    # 3 天内不重复分析
    assert run_spread(days=3, analyze_gaps=True)["analyzed"] == {"skipped": 3}


@pytest.mark.db
def test_import_export_and_backtest(db, tmp_path):
    from groundswell.dataio import export_jsonl, import_jsonl

    start = date(2024, 3, 1) - timedelta(days=120)
    lines = []
    for i in range(140):
        day = datetime.combine(start + timedelta(days=i), datetime.min.time(), tzinfo=UTC)
        for j in range(20 if i >= 110 else 1):
            lines.append(
                {
                    "title": f"widgetx launch thread {i}-{j}",
                    "published_at": (day + timedelta(hours=12, minutes=j)).isoformat(),
                }
            )
    lines.append({"title": "缺少时间"})
    src = tmp_path / "hist.jsonl"
    src.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")
    n = import_jsonl(src, source_id="hist", region="GLOBAL")
    assert n == len(lines) - 1
    assert import_jsonl(src, source_id="hist", region="GLOBAL") == 0  # 幂等
    with session_scope() as s:
        snap_ts = s.execute(text("SELECT min(ts) FROM item_snapshots")).scalar_one()
    assert snap_ts.date() == start  # 历史数据落在自己的时间点上

    out = tmp_path / "export.jsonl"
    assert export_jsonl(out, days=365 * 5) == n

    golden = tmp_path / "golden.yaml"
    golden.write_text(
        "version: t1\ncases:\n"
        "  - {name: WidgetX, keywords: [widgetx], region: GLOBAL, breakout: 2024-03-01, approx: false}\n"
        "  - {name: Nothing, keywords: [nothingatall], region: NA, breakout: 2024-03-01}\n",
        encoding="utf-8",
    )
    path, summary = run_backtest(golden_path=golden, lookback_days=120, out_dir=tmp_path / "bt")
    assert summary["hits"] == 1 and summary["with_data"] == 1 and summary["avg_lead_days"] == 10
    assert summary["regression"] is False
    assert "WidgetX" in path.read_text(encoding="utf-8")
    kpi = (tmp_path / "bt" / "kpi.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(kpi[-1])["hit_rate"] == 1.0
