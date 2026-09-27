"""四级区域归属：源级 → 子频道 → 内容级 → GLOBAL 兜底。

前两级在入库时已完成（collectors.runner.resolve_region）。这里处理全球源上的内容级判断：
国家别名（地名、货币、平台名）为强证据，语言为弱证据（英文、西语横跨多个区域）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from groundswell.configs import GLOBAL, RegionsFile

# 语言 → 区域的弱证据权重；en 不提供区域信息
LANGUAGE_HINTS: dict[str, dict[str, float]] = {
    "zh": {"GC": 0.45},
    "th": {"SEA": 0.6},
    "vi": {"SEA": 0.6},
    "id": {"SEA": 0.55},
    "tl": {"SEA": 0.55},
    "ms": {"SEA": 0.5},
    "pt": {"LATAM": 0.4},
    "es": {"LATAM": 0.3, "EU": 0.15},
    "de": {"EU": 0.5},
    "fr": {"EU": 0.4},
    "it": {"EU": 0.5},
    "nl": {"EU": 0.5},
    "sv": {"EU": 0.5},
    "pl": {"EU": 0.5},
}

ALIAS_WEIGHT = 0.6
PRIMARY_THRESHOLD = 0.5
SECONDARY_THRESHOLD = 0.3


@dataclass
class Attribution:
    region: str
    country: str | None
    method: str
    confidence: float
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class _AliasMatcher:
    patterns: tuple[tuple[re.Pattern[str], str, str], ...]  # (pattern, country, region)

    def match(self, text: str) -> dict[str, set[str]]:
        hits: dict[str, set[str]] = {}
        for pattern, country, region in self.patterns:
            if pattern.search(text):
                hits.setdefault(region, set()).add(country)
        return hits


def _compile_alias(alias: str) -> re.Pattern[str]:
    if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", alias):
        return re.compile(re.escape(alias))
    flags = 0 if (alias.isupper() and len(alias) <= 3) else re.IGNORECASE
    return re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", flags)


_MATCHERS: dict[int, tuple[RegionsFile, _AliasMatcher]] = {}


def _matcher(regions: RegionsFile) -> _AliasMatcher:
    cached = _MATCHERS.get(id(regions))
    if cached and cached[0] is regions:
        return cached[1]
    patterns = []
    for code, region in regions.regions.items():
        for cc, country in region.countries.items():
            for alias in country.aliases:
                patterns.append((_compile_alias(alias), cc, code))
    matcher = _AliasMatcher(tuple(patterns))
    _MATCHERS[id(regions)] = (regions, matcher)
    return matcher


def attribute_content(text: str, language: str | None, regions: RegionsFile) -> Attribution:
    matcher = _matcher(regions)
    weights: dict[str, float] = {}
    countries: dict[str, set[str]] = matcher.match(text)
    for region in countries:
        weights[region] = weights.get(region, 0.0) + ALIAS_WEIGHT
    for region, w in LANGUAGE_HINTS.get(language or "", {}).items():
        weights[region] = weights.get(region, 0.0) + w
    weights = {r: min(w, 1.0) for r, w in weights.items() if r != GLOBAL}
    if not weights:
        return Attribution(GLOBAL, None, "fallback", 0.0)
    best, conf = max(weights.items(), key=lambda kv: kv[1])
    kept = {r: round(w, 3) for r, w in weights.items() if w >= SECONDARY_THRESHOLD}
    if conf < PRIMARY_THRESHOLD:
        return Attribution(GLOBAL, None, "fallback", conf, kept)
    country = next(iter(sorted(countries.get(best, set()))), None)
    return Attribution(best, country, "content", round(conf, 3), kept)


def attribute(
    *,
    ingest_region: str | None,
    ingest_country: str | None,
    ingest_method: str | None,
    ingest_confidence: float | None,
    text: str,
    language: str | None,
    regions: RegionsFile,
) -> Attribution:
    """入库时已确定非 GLOBAL 区域则保留；全球源再做内容级判断。"""
    if ingest_region and ingest_region != GLOBAL:
        conf = ingest_confidence if ingest_confidence is not None else 1.0
        return Attribution(
            ingest_region, ingest_country, ingest_method or "source", conf, {ingest_region: conf}
        )
    return attribute_content(text, language, regions)
