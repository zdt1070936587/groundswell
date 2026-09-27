"""Streamlit 内部看板 v0：排行榜 / 话题详情 / 商机 / 信息差矩阵 / 点子库 / 源健康。

运行：streamlit run src/groundswell/dashboard/streamlit_app.py
（生产环境由 compose 的 dashboard profile 启动，前面务必加反向代理鉴权）
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

from groundswell import queries
from groundswell.config import get_settings
from groundswell.configs import load_regions
from groundswell.db.session import session_scope
from groundswell.processing.timeutil import now_utc
from groundswell.taxonomy import (
    BUSINESS_CATEGORIES,
    CATEGORY_LABELS,
    DURABILITY_LABELS,
    IDEA_STATUS_LABELS,
    IDEA_STATUSES,
)

st.set_page_config(page_title="观潮 Groundswell", layout="wide")
settings = get_settings()
regions = load_regions()
REGION_CODES = list(regions.regions)


def region_label(code: str) -> str:
    r = regions.regions.get(code)
    return f"{code} · {r.name}" if r else code


@st.cache_data(ttl=300)
def load_rankings(region: str, sort: str, hours: int, business: bool) -> dict:
    end = now_utc()
    start = end - timedelta(hours=hours)
    with session_scope() as s:
        topics = queries.topic_ranking(
            s,
            region,
            start,
            end,
            sort=sort,
            limit=50,
            categories=BUSINESS_CATEGORIES if business else None,
            z_threshold=settings.trend_z_threshold if sort == "growth" else None,
        )
        items = [] if topics else queries.item_heat(s, region, start, end, limit=50)
    return {"topics": topics, "items": items}


def page_rankings() -> None:
    st.header("排行榜")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    region = c1.selectbox("区域", REGION_CODES, format_func=region_label)
    sort = c2.radio(
        "排序",
        ["heat", "growth", "mentions"],
        horizontal=True,
        format_func={"heat": "热度", "growth": "增长", "mentions": "提及"}.get,
    )
    hours = c3.select_slider(
        "窗口", options=[6, 12, 24, 48, 72, 168], value=24, format_func=lambda h: f"{h}h"
    )
    business = c4.checkbox("仅业务类", value=sort == "growth")
    data = load_rankings(region, sort, hours, business)
    if data["topics"]:
        df = pd.DataFrame(data["topics"])
        df["类别"] = df["category"].map(lambda c: CATEGORY_LABELS.get(c, "—"))
        st.dataframe(
            df[
                [
                    "id",
                    "name",
                    "类别",
                    "mentions",
                    "engagement",
                    "max_z",
                    "max_score",
                    "sources",
                    "item_count",
                    "active_regions",
                ]
            ],
            hide_index=True,
            width="stretch",
            column_config={"name": st.column_config.TextColumn("话题", width="large")},
        )
        st.caption("在「话题详情」输入 id 查看时间序列与原文。")
    elif data["items"]:
        st.info("尚无话题统计（embedding / 归并未运行），显示条目级热度。")
        st.dataframe(pd.DataFrame(data["items"]), hide_index=True, width="stretch")
    else:
        st.warning("窗口内无数据")


def page_topic() -> None:
    st.header("话题详情")
    tid = st.number_input("话题 id", min_value=1, step=1, value=st.session_state.get("topic_id", 1))
    with session_scope() as s:
        detail = queries.topic_detail(s, int(tid))
        if not detail:
            st.warning("话题不存在")
            return
        series = queries.topic_series(s, int(tid), hours=24 * 7)
        similar = queries.similar_topics(s, int(tid))
    st.subheader(detail["name"])
    if detail.get("status") == "merged":
        st.info(f"该话题已合并到 #{detail['merged_into']}")
    cols = st.columns(5)
    cols[0].metric("类别", CATEGORY_LABELS.get(detail["category"], "—"))
    cols[1].metric("条目", detail["item_count"])
    cols[2].metric("源数", detail["source_count"])
    cols[3].metric("起源区域", detail.get("origin_region") or "—")
    cols[4].metric("活跃区域", ", ".join(detail.get("active_regions") or []) or "—")
    if detail.get("summary"):
        st.write(detail["summary"])
    st.write("关键词：", "、".join(detail.get("keywords") or []))
    if detail.get("slow_variables"):
        st.write("慢变量：", "、".join(detail["slow_variables"]))
    if detail.get("spread_path"):
        st.write("扩散路径：", " → ".join(p.get("region", "?") for p in detail["spread_path"]))
    if series:
        df = pd.DataFrame(series)
        metric = st.radio("指标", ["mentions", "engagement", "z"], horizontal=True)
        st.plotly_chart(px.line(df, x="hour", y=metric, color="region"), width="stretch")
    st.subheader("原文")
    st.dataframe(
        pd.DataFrame(detail["items"])[
            ["title", "source_id", "region", "language", "fetched_at", "url"]
        ],
        hide_index=True,
        width="stretch",
        column_config={"url": st.column_config.LinkColumn("链接")},
    )
    if detail["opportunities"]:
        st.subheader("商机分析")
        for o in detail["opportunities"]:
            render_opportunity(o)
    if similar:
        st.subheader("历史相似话题")
        st.dataframe(pd.DataFrame(similar), hide_index=True, width="stretch")


def render_opportunity(o: dict) -> None:
    title = f"[{o['region']}] {o['topic_name']} — {o['total_score']:.1f}"
    if o.get("is_gap"):
        title += " · 区域空白"
    with st.expander(title):
        st.write(o.get("summary") or "")
        st.markdown(f"**为什么是现在**：{o.get('why_now') or '—'}")
        st.markdown(f"**分发路径**：{o.get('distribution_path') or '—'}")
        st.markdown(f"**持续性**：{DURABILITY_LABELS.get(o.get('durability') or 'unknown')}")
        if o.get("scores"):
            st.bar_chart(pd.Series(o["scores"], name="分数"))
        for label, key in (
            ("产品形态", "product_shapes"),
            ("反方论点", "counter_arguments"),
            ("已知玩家", "known_players"),
        ):
            if o.get(key):
                st.markdown(f"**{label}**")
                for x in o[key]:
                    st.markdown(f"- {x if isinstance(x, str) else x}")
        with session_scope() as s:
            ev = queries.evidence_for(s, o["id"])
        if ev:
            st.markdown("**证据**")
            for e in ev:
                st.markdown(f"- [{e['snippet'][:80]}]({e['url']}) `{e['source_id']}`")
        c1, c2, c3, c4 = st.columns(4)
        from groundswell.ideas import add_from_opportunity, record_feedback

        for col, label in ((c1, "good"), (c2, "meh"), (c3, "bad")):
            if col.button(label, key=f"fb-{o['id']}-{label}"):
                record_feedback(o["id"], label, user="dashboard")
                st.success(f"已反馈 {label}")
        if c4.button("加入点子库", key=f"idea-{o['id']}"):
            st.success(f"点子 #{add_from_opportunity(o['id'], user='dashboard')}")


def page_opportunities() -> None:
    st.header("商机")
    c1, c2 = st.columns(2)
    region = c1.selectbox(
        "区域", ["全部", *REGION_CODES], format_func=lambda c: c if c == "全部" else region_label(c)
    )
    days = c2.slider("最近天数", 1, 30, 7)
    with session_scope() as s:
        opps = queries.opportunities(
            s, region=None if region == "全部" else region, days=days, limit=100
        )
    if not opps:
        st.info("暂无商机（需要运行 gs score；未配置 LLM 时为启发式分析）")
    for o in opps:
        render_opportunity(o)


def page_matrix() -> None:
    st.header("信息差矩阵")
    days = st.slider("最近天数", 1, 30, 7)
    with session_scope() as s:
        rows = queries.region_matrix(
            s, [r for r in REGION_CODES if r != "GLOBAL"], days=days, limit=40
        )
    if not rows:
        st.info("暂无跨区话题")
        return
    df = pd.DataFrame(
        [{"话题": f"#{r['id']} {r['name'][:30]}", **(r["z_by_region"] or {})} for r in rows]
    ).set_index("话题")
    st.plotly_chart(
        px.imshow(df, color_continuous_scale="RdYlGn_r", aspect="auto", text_auto=".1f"),
        width="stretch",
    )
    st.caption("颜色越红 = 该区域越显著起飞；空白 = 该区域尚无讨论。")


def page_ideas() -> None:
    from groundswell.ideas import add_comment, get_idea, list_ideas, move_idea

    st.header("点子库")
    status = st.radio(
        "状态",
        ["全部", *IDEA_STATUSES],
        horizontal=True,
        format_func=lambda s: IDEA_STATUS_LABELS.get(s, s),
    )
    ideas = list_ideas(None if status == "全部" else status)
    if not ideas:
        st.info("点子库为空：对商机反馈 good 会自动加入")
        return
    for idea in ideas:
        with st.expander(
            f"#{idea['id']} [{IDEA_STATUS_LABELS[idea['status']]}] {idea['region']} {idea['title']}"
        ):
            full = get_idea(idea["id"]) or {}
            st.write(f"负责人：{idea.get('owner') or '—'}")
            for ev in full.get("events", []):
                st.caption(
                    f"{ev['ts']:%m-%d %H:%M} {ev['from_status'] or '∅'} → {ev['to_status']} {ev['note'] or ''}"
                )
            for cm in full.get("comments", []):
                st.markdown(f"> **{cm['user_name']}**：{cm['body']}")
            c1, c2 = st.columns(2)
            new = c1.selectbox(
                "移动到",
                IDEA_STATUSES,
                index=IDEA_STATUSES.index(idea["status"]),
                key=f"mv-{idea['id']}",
                format_func=IDEA_STATUS_LABELS.get,
            )
            if new != idea["status"] and c1.button("确认", key=f"mvb-{idea['id']}"):
                move_idea(idea["id"], new, user="dashboard")
                st.rerun()
            body = c2.text_input("评论", key=f"cm-{idea['id']}")
            if body and c2.button("发表", key=f"cmb-{idea['id']}"):
                add_comment(idea["id"], body, user="dashboard")
                st.rerun()


def page_health() -> None:
    st.header("数据源健康")
    with session_scope() as s:
        rows = queries.source_health(s, hours=24)
        cost = queries.llm_cost(s, now_utc() - timedelta(hours=24), now_utc())
    c1, c2 = st.columns(2)
    c1.metric("24h LLM 调用", cost["calls"])
    c2.metric("24h LLM 花费", f"¥{float(cost['cost_usd']) * settings.usd_to_cny:.2f}")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


PAGES = {
    "排行榜": page_rankings,
    "话题详情": page_topic,
    "商机": page_opportunities,
    "信息差矩阵": page_matrix,
    "点子库": page_ideas,
    "源健康": page_health,
}

st.sidebar.title("观潮 Groundswell")
choice = st.sidebar.radio("页面", list(PAGES))
PAGES[choice]()
