"""关键词抽取：用于聚类之前的关键词级热度，以及话题的关键词摘要。"""

from __future__ import annotations

import logging
import re
from functools import lru_cache

from groundswell.processing.lang import STOPWORDS

MAX_KEYWORDS = 10

_EXTRA_STOP_EN = set(
    [
        "show",
        "ask",
        "tell",
        "hn",
        "new",
        "using",
        "use",
        "used",
        "get",
        "got",
        "one",
        "two",
        "just",
        "like",
        "make",
        "made",
        "way",
        "first",
        "best",
        "top",
        "now",
        "today",
        "year",
        "years",
        "day",
        "days",
        "time",
        "week",
        "weeks",
        "month",
        "vs",
        "via",
        "its",
        "it's",
        "his",
        "her",
        "their",
        "our",
        "out",
        "more",
        "most",
        "than",
        "then",
        "them",
        "they",
        "she",
        "him",
        "has",
        "had",
        "been",
        "being",
        "does",
        "did",
        "doing",
        "over",
        "under",
        "after",
        "before",
        "why",
        "what",
        "how",
        "who",
        "when",
        "where",
        "which",
        "there",
        "here",
        "these",
        "those",
        "some",
        "any",
        "all",
        "every",
        "each",
        "video",
        "watch",
        "live",
        "update",
        "updates",
        "news",
        "report",
        "says",
        "said",
        "official",
        "app",
        "apps",
        "free",
        "review",
        "reddit",
        "post",
        "thread",
        "question",
        "help",
        "anyone",
        "need",
        "looking",
        "guide",
        "tips",
        "people",
        "thing",
        "things",
        "discussion",
        "link",
        "comments",
        "inc",
        "ltd",
        "llc",
        "pte",
        "corp",
        "corporation",
        "gmbh",
        "limited",
        "co",
        "company",
        "technology",
        "technologies",
    ]
)

_ZH_STOP = set(
    [
        "回应",
        "网友",
        "视频",
        "官方",
        "什么",
        "为什么",
        "如何",
        "怎么",
        "已经",
        "今天",
        "最新",
        "热搜",
        "曝光",
        "发布",
        "推荐",
        "一个",
        "这个",
        "那个",
        "我们",
        "你们",
        "他们",
        "自己",
        "没有",
        "可以",
        "还是",
        "就是",
        "真的",
        "不是",
        "现在",
        "时候",
        "知道",
        "问题",
        "事情",
        "情况",
        "中国",
        "表示",
        "进行",
        "相关",
        "目前",
        "记者",
        "消息",
        "称",
        "后",
        "将",
        "被",
        "等",
        "年",
        "月",
        "日",
        "万",
        "亿",
        "岁",
        "有限公司",
        "公司",
        "科技有限公司",
    ]
)

_LATIN_TOKEN = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9+#.\-]*[A-Za-zÀ-ÿ0-9+#]|[A-Za-zÀ-ÿ]")
_ALL_STOP = set().union(*STOPWORDS.values()) | _EXTRA_STOP_EN


@lru_cache(maxsize=1)
def _jieba():
    import jieba
    import jieba.analyse

    jieba.setLogLevel(logging.WARNING)
    return jieba.analyse


def _latin_keywords(text: str) -> list[str]:
    raw = _LATIN_TOKEN.findall(text)
    tokens: list[str | None] = []
    for tok in raw:
        low = tok.lower().strip(".-")
        keep = (
            low
            and low not in _ALL_STOP
            and not low.isdigit()
            and (len(low) >= 3 or (tok.isupper() and len(tok) >= 2))
        )
        tokens.append(low if keep else None)
    out: list[str] = []
    for a, b in zip(tokens, tokens[1:], strict=False):
        if a and b:
            out.append(f"{a} {b}")
    out.extend(t for t in tokens if t)
    return out


def _zh_keywords(text: str) -> list[str]:
    tags = _jieba().extract_tags(text, topK=8)
    out = [
        t
        for t in tags
        if len(t) >= 2 and t not in _ZH_STOP and not t.isdigit() and t.lower() not in _ALL_STOP
    ]
    # 中文标题里夹带的英文专名（iPhone、DeepSeek）
    out.extend(k for k in _latin_keywords(text) if " " not in k)
    return out


def _thai_keywords(text: str) -> list[str]:
    try:
        from pythainlp.tokenize import word_tokenize
    except ImportError:
        return _latin_keywords(text)
    return [w for w in word_tokenize(text, engine="newmm") if len(w) >= 2 and w.strip()]


def extract_keywords(title: str, text: str | None = None, language: str | None = None) -> list[str]:
    """优先使用标题；标题过短时补充正文前 200 字。"""
    body = title if len(title) >= 12 or not text else f"{title} {text[:200]}"
    if language in ("zh", "ja"):
        kws = _zh_keywords(body)
    elif language == "th":
        kws = _thai_keywords(body)
    else:
        kws = _latin_keywords(body)
    seen: dict[str, None] = {}
    for k in kws:
        k = k.strip().lower()[:128]
        if k and k not in seen:
            seen[k] = None
    return list(seen)[:MAX_KEYWORDS]
