"""六类话题分类：娱乐 / 社会 / 需求 / 技术 / 消费 / 政策。

小时级用启发式（词典 + 源的 category_hint 投票）快速给出类别；每日命名时若有 LLM 会覆盖为模型判断。
商机漏斗只看 demand / tech / consumer / policy，娱乐与社会类在进入 LLM 之前就被过滤，节省成本。
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import text
from sqlalchemy.orm import Session

from groundswell.configs import load_sources
from groundswell.processing.terms import count_matches
from groundswell.processing.timeutil import now_utc
from groundswell.taxonomy import CATEGORIES

LEXICON: dict[str, tuple[str, ...]] = {
    "entertainment": [
        "明星",
        "综艺",
        "电影",
        "电视剧",
        "演唱会",
        "恋情",
        "官宣",
        "离婚",
        "塌房",
        "偶像",
        "选秀",
        "票房",
        "导演",
        "演员",
        "歌手",
        "追星",
        "剧集",
        "番剧",
        "动漫",
        "主演",
        "粉丝",
        "热播",
        "首映",
        "爆料",
        "绯闻",
        "cp",
        "movie",
        "film",
        "celebrity",
        "singer",
        "actor",
        "actress",
        "album",
        "concert",
        "trailer",
        "drama",
        "netflix",
        "episode",
        "oscar",
        "grammy",
        "kpop",
        "anime",
        "tv",
        "series",
        "box",
        "office",
        "premiere",
    ],
    "society": [
        "警方",
        "事故",
        "死亡",
        "地震",
        "台风",
        "暴雨",
        "火灾",
        "通报",
        "学校",
        "教师",
        "学生",
        "医院",
        "法院",
        "判决",
        "犯罪",
        "失踪",
        "救援",
        "遇难",
        "伤亡",
        "殴打",
        "网暴",
        "考试",
        "高考",
        "天气",
        "气温",
        "比赛",
        "夺冠",
        "冠军",
        "足球",
        "篮球",
        "奥运",
        "亚运",
        "全运会",
        "夺金",
        "金牌",
        "银牌",
        "铜牌",
        "田径",
        "乒乓",
        "女单",
        "男单",
        "破纪录",
        "军事",
        "外交",
        "会晤",
        "元首",
        "台海",
        "动物园",
        "大熊猫",
        "police",
        "killed",
        "shooting",
        "earthquake",
        "storm",
        "flood",
        "crash",
        "court",
        "trial",
        "arrested",
        "death",
        "dies",
        "weather",
        "match",
        "vs",
        "football",
        "soccer",
        "nba",
        "nfl",
        "game",
        "score",
        "war",
        "attack",
        "news",
        "playing",
        "today",
        "highway",
    ],
    "demand": [
        "求推荐",
        "有没有",
        "怎么办",
        "吐槽",
        "痛点",
        "难用",
        "希望有",
        "需求",
        "求助",
        "替代",
        "太贵",
        "不好用",
        "求问",
        "避坑",
        "alternative",
        "recommend",
        "struggle",
        "frustrated",
        "wish",
        "need",
        "looking",
        "ask",
        "feature",
        "request",
        "pain",
        "hate",
        "annoying",
        "workaround",
    ],
    "tech": [
        "ai",
        "大模型",
        "模型",
        "开源",
        "芯片",
        "算法",
        "编程",
        "代码",
        "开发者",
        "github",
        "agent",
        "智能体",
        "机器人",
        "自动驾驶",
        "量子",
        "算力",
        "gpu",
        "半导体",
        "操作系统",
        "鸿蒙",
        "llm",
        "api",
        "developer",
        "python",
        "rust",
        "javascript",
        "typescript",
        "framework",
        "open-source",
        "startup",
        "robot",
        "quantum",
        "database",
        "cloud",
        "saas",
        "chip",
        "model",
        "inference",
        "cursor",
    ],
    "consumer": [
        "新品",
        "品牌",
        "消费",
        "价格",
        "优惠",
        "购物",
        "电商",
        "外卖",
        "奶茶",
        "咖啡",
        "旅游",
        "景区",
        "酒店",
        "美妆",
        "平价",
        "爆款",
        "开箱",
        "门店",
        "手机",
        "汽车",
        "新能源",
        "直播带货",
        "团购",
        "app",
        "price",
        "deal",
        "shop",
        "store",
        "brand",
        "buy",
        "sale",
        "discount",
        "travel",
        "food",
        "restaurant",
        "coffee",
        "fashion",
        "beauty",
        "subscription",
        "iphone",
        "car",
        "ev",
    ],
    "policy": [
        "政策",
        "国务院",
        "发改委",
        "部委",
        "监管",
        "法规",
        "条例",
        "印发",
        "意见",
        "税",
        "补贴",
        "关税",
        "央行",
        "利率",
        "立法",
        "试点",
        "规划",
        "regulation",
        "law",
        "bill",
        "congress",
        "senate",
        "tariff",
        "tax",
        "ban",
        "ministry",
        "government",
        "policy",
        "fda",
        "sec",
        "act",
        "court",
        "ruling",
        "sanction",
    ],
}

HINT_WEIGHT = 2.0


def classify_text(text_: str, source_hints: list[str] | None = None) -> tuple[str | None, float]:
    scores: Counter[str] = Counter()
    for cat, words in LEXICON.items():
        scores[cat] = count_matches(text_, words)
    for hint in source_hints or []:
        if hint in CATEGORIES:
            scores[hint] += HINT_WEIGHT
    total = sum(scores.values())
    if total <= 0:
        return None, 0.0
    best, val = scores.most_common(1)[0]
    return best, round(min(0.9, val / total), 3)


def _source_hints() -> dict[str, str]:
    return {s.id: s.category_hint for s in load_sources().sources if s.category_hint}


def classify_topics(session: Session, limit: int = 2000) -> int:
    """未分类、或启发式分类后又有新条目的话题；LLM 命名过的话题不覆盖。"""
    rows = session.execute(
        text(
            """
            SELECT t.id, t.name, t.keywords,
                   (SELECT array_agg(DISTINCT i.source_id) FROM topic_items ti
                      JOIN raw_items i ON i.id = ti.item_id WHERE ti.topic_id = t.id) AS sources,
                   (SELECT string_agg(title, ' ') FROM (
                        SELECT i.title FROM topic_items ti JOIN raw_items i ON i.id = ti.item_id
                        WHERE ti.topic_id = t.id ORDER BY i.id DESC LIMIT 5) x) AS titles
            FROM topics t
            WHERE t.status = 'active'
              AND (t.classified_at IS NULL
                   OR (t.named_at IS NULL AND t.classified_at < t.last_seen - interval '6 hours'))
            ORDER BY t.last_seen DESC LIMIT :limit
            """
        ),
        {"limit": limit},
    ).all()
    if not rows:
        return 0
    hints = _source_hints()
    now = now_utc()
    updates = []
    for r in rows:
        src_hints = [hints[s] for s in (r.sources or []) if s in hints]
        body = " ".join([r.name or "", " ".join(r.keywords or []), r.titles or ""])
        cat, conf = classify_text(body, src_hints)
        updates.append({"id": r.id, "c": cat, "conf": conf, "ts": now})
    session.execute(
        text(
            "UPDATE topics SET category = :c, category_confidence = :conf, classified_at = :ts WHERE id = :id"
        ),
        updates,
    )
    return len(updates)
