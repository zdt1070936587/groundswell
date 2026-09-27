"""话题分类与标签的统一定义。"""

from __future__ import annotations

CATEGORIES = ("entertainment", "society", "demand", "tech", "consumer", "policy")
BUSINESS_CATEGORIES = ("demand", "tech", "consumer", "policy")
NOISE_CATEGORIES = ("entertainment", "society")

CATEGORY_LABELS = {
    "entertainment": "娱乐",
    "society": "社会",
    "demand": "需求",
    "tech": "技术",
    "consumer": "消费",
    "policy": "政策",
    None: "未分类",
}

REGION_ORDER = ("GLOBAL", "GC", "NA", "SEA", "LATAM", "EU")

FEEDBACK_LABELS = ("good", "meh", "bad")
IDEA_STATUSES = ("candidate", "watching", "validated", "archived")
IDEA_STATUS_LABELS = {
    "candidate": "候选",
    "watching": "观察",
    "validated": "已验证",
    "archived": "归档",
}

DURABILITY_LABELS = {
    "one_off": "一次性热点",
    "seasonal": "季节性",
    "structural": "结构性趋势",
    "unknown": "未知",
}
