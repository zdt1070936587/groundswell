"""词项匹配：拉丁词按词边界匹配（避免 "ev" 命中 "every"），中日韩文按子串匹配。"""

from __future__ import annotations

import re
from functools import lru_cache

_HAS_LATIN_ONLY = re.compile(r"^[\x00-\x7f]+$")


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern[str]:
    t = term.strip().lower()
    if _HAS_LATIN_ONLY.match(t):
        return re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])")
    return re.compile(re.escape(t))


def matches_any(text: str, terms: list[str] | tuple[str, ...]) -> bool:
    low = text.lower()
    return any(term_pattern(t).search(low) for t in terms if t.strip())


def count_matches(text: str, terms: list[str] | tuple[str, ...]) -> int:
    low = text.lower()
    return sum(1 for t in terms if t.strip() and term_pattern(t).search(low))
