"""轻量语言识别：先看文字系统，拉丁字母再按停用词打分。

短标题上统计模型也不可靠，因此这里只作为区域归属的弱证据，源级语言优先。
"""

from __future__ import annotations

import re

_SCRIPTS = [
    ("ja", re.compile(r"[\u3040-\u30ff]")),
    ("ko", re.compile(r"[\uac00-\ud7af]")),
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("th", re.compile(r"[\u0e00-\u0e7f]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
]
# 只保留越南语独有的字母（â ê ô 在葡语/法语里也常见）
_VI = re.compile(r"[ăđơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.IGNORECASE)
_WORD = re.compile(r"[a-zà-ÿ]+", re.IGNORECASE)
_MARKS = {"pt": ("ã", "õ", "ç", "ê"), "es": ("ñ", "¿", "¡"), "de": ("ß", "ü", "ö", "ä")}

STOPWORDS: dict[str, set[str]] = {
    "en": set(
        [
            "the",
            "and",
            "for",
            "with",
            "that",
            "this",
            "from",
            "you",
            "your",
            "are",
            "was",
            "have",
            "how",
            "what",
            "why",
            "who",
            "will",
            "can",
            "not",
            "new",
            "about",
            "into",
            "to",
            "of",
            "in",
            "is",
            "on",
            "it",
            "my",
            "i",
            "a",
            "an",
            "by",
            "at",
            "be",
            "or",
            "as",
            "we",
        ]
    ),
    "es": set(
        [
            "el",
            "la",
            "los",
            "las",
            "de",
            "del",
            "que",
            "y",
            "en",
            "un",
            "una",
            "por",
            "para",
            "con",
            "es",
            "se",
            "no",
            "lo",
            "más",
            "como",
            "pero",
            "sus",
            "al",
        ]
    ),
    "pt": set(
        [
            "o",
            "a",
            "os",
            "as",
            "de",
            "do",
            "da",
            "dos",
            "das",
            "que",
            "e",
            "em",
            "um",
            "uma",
            "para",
            "com",
            "não",
            "é",
            "se",
            "na",
            "no",
            "mais",
            "por",
            "ao",
        ]
    ),
    "fr": set(
        [
            "le",
            "la",
            "les",
            "des",
            "de",
            "du",
            "et",
            "en",
            "un",
            "une",
            "pour",
            "avec",
            "est",
            "pas",
            "que",
            "qui",
            "dans",
            "sur",
            "au",
            "aux",
            "ce",
            "il",
        ]
    ),
    "de": set(
        [
            "der",
            "die",
            "das",
            "und",
            "ist",
            "nicht",
            "ein",
            "eine",
            "zu",
            "mit",
            "von",
            "den",
            "im",
            "für",
            "auf",
            "dem",
            "sich",
            "des",
            "auch",
        ]
    ),
    "it": set(
        [
            "il",
            "lo",
            "la",
            "gli",
            "le",
            "di",
            "che",
            "e",
            "un",
            "una",
            "per",
            "con",
            "non",
            "è",
            "del",
            "della",
            "sono",
            "nel",
            "alla",
            "anche",
        ]
    ),
    "nl": set(
        [
            "de",
            "het",
            "een",
            "en",
            "van",
            "is",
            "dat",
            "niet",
            "op",
            "te",
            "zijn",
            "voor",
            "met",
            "die",
            "aan",
            "er",
            "maar",
            "ook",
        ]
    ),
    "id": set(
        [
            "yang",
            "dan",
            "di",
            "ini",
            "itu",
            "untuk",
            "dengan",
            "tidak",
            "dari",
            "ada",
            "ke",
            "akan",
            "juga",
            "pada",
            "saya",
            "bisa",
        ]
    ),
    "tl": set(
        [
            "ang",
            "ng",
            "mga",
            "sa",
            "na",
            "at",
            "ay",
            "ko",
            "mo",
            "siya",
            "hindi",
            "ako",
            "para",
            "kung",
        ]
    ),
    "sv": set(
        [
            "och",
            "att",
            "det",
            "som",
            "en",
            "på",
            "är",
            "för",
            "med",
            "inte",
            "av",
            "till",
            "den",
            "har",
            "jag",
        ]
    ),
    "pl": set(
        [
            "i",
            "w",
            "nie",
            "na",
            "się",
            "że",
            "z",
            "do",
            "jest",
            "to",
            "jak",
            "po",
            "co",
            "ale",
            "dla",
        ]
    ),
}


def detect_language(text: str | None) -> str:
    if not text:
        return "und"
    for code, pattern in _SCRIPTS:
        # 日文含汉字，需先于中文判断假名
        if pattern.search(text):
            return code
    if _VI.search(text):
        return "vi"
    words = [w.lower() for w in _WORD.findall(text)]
    if not words:
        return "und"
    scores = {lang: float(sum(w in sw for w in words)) for lang, sw in STOPWORDS.items()}
    lowered = text.lower()
    for lang, marks in _MARKS.items():
        if any(m in lowered for m in marks):
            scores[lang] += 1.5
    best, hits = max(scores.items(), key=lambda kv: kv[1])
    if hits == 0:
        return "en" if all(ord(c) < 128 for c in text) else "und"
    return best
