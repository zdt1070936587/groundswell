"""simhash 近重复检测：同一篇内容在多个源/镜像重复出现时只保留最早一条参与 embedding 与证据。

重复条目仍计入热度统计（跨平台转载本身就是热度信号），只是不重复消耗 embedding / LLM。
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af\u0e00-\u0e7f]")
_WORD = re.compile(r"\w+", re.UNICODE)
_NORM = re.compile(r"[\W_]+", re.UNICODE)

BANDS = 4
BAND_BITS = 16
MIN_FEATURES = 4


def features(text: str) -> list[str]:
    text = text.lower()
    feats: list[str] = []
    cjk = "".join(_CJK.findall(text))
    feats.extend(cjk[i : i + 2] for i in range(max(len(cjk) - 1, 0)))
    words = [w for w in _WORD.findall(_CJK.sub(" ", text)) if len(w) > 1]
    feats.extend(words)
    feats.extend(f"{a} {b}" for a, b in zip(words, words[1:], strict=False))
    return feats


_SHIFTS = np.arange(64, dtype=np.uint64)


def simhash(text: str) -> int:
    """64 位无符号 simhash。"""
    feats = features(text)
    if not feats:
        return 0
    hashes = np.array(
        [int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big") for f in feats],
        dtype=np.uint64,
    )
    bits = ((hashes[:, None] >> _SHIFTS) & np.uint64(1)).astype(np.int32)
    votes = (bits * 2 - 1).sum(axis=0)
    return sum(1 << i for i in range(64) if votes[i] > 0)


def to_signed(x: int) -> int:
    return x - (1 << 64) if x >= (1 << 63) else x


def to_unsigned(x: int) -> int:
    return x + (1 << 64) if x < 0 else x


def hamming(a: int, b: int) -> int:
    return bin(to_unsigned(a) ^ to_unsigned(b)).count("1")


def normalize_title(title: str) -> str:
    return _NORM.sub("", title.lower())


@dataclass(frozen=True)
class Fingerprint:
    item_id: int
    simhash: int
    norm_title: str
    n_features: int


def fingerprint(item_id: int, title: str, text: str | None = None) -> Fingerprint:
    body = f"{title} {text[:300]}" if text else title
    return Fingerprint(item_id, simhash(body), normalize_title(title), len(features(body)))


class DuplicateIndex:
    """鸽笼原理：海明距离 ≤ 3 时 4 个 16 位分段至少有一段完全相同。"""

    def __init__(self, max_distance: int = 3):
        self.max_distance = max_distance
        self._bands: dict[tuple[int, int], list[Fingerprint]] = defaultdict(list)
        self._titles: dict[str, int] = {}

    def add(self, fp: Fingerprint) -> None:
        u = to_unsigned(fp.simhash)
        for b in range(BANDS):
            self._bands[(b, (u >> (b * BAND_BITS)) & 0xFFFF)].append(fp)
        if fp.norm_title:
            self._titles.setdefault(fp.norm_title, fp.item_id)

    def find(self, fp: Fingerprint) -> int | None:
        # 短文本 simhash 不稳定，只认标题完全一致
        exact = self._titles.get(fp.norm_title) if fp.norm_title else None
        if exact is not None and exact != fp.item_id:
            return exact
        if fp.n_features < MIN_FEATURES:
            return None
        u = to_unsigned(fp.simhash)
        best: tuple[int, int] | None = None
        for b in range(BANDS):
            for cand in self._bands.get((b, (u >> (b * BAND_BITS)) & 0xFFFF), ()):
                if cand.item_id == fp.item_id or cand.n_features < MIN_FEATURES:
                    continue
                d = hamming(fp.simhash, cand.simhash)
                if d <= self.max_distance and (best is None or (d, cand.item_id) < best):
                    best = (d, cand.item_id)
        return best[1] if best else None


def find_duplicates(existing: Iterable[Fingerprint], new: Iterable[Fingerprint]) -> dict[int, int]:
    """返回 {新条目 id: 被重复的更早条目 id}。new 按 id 升序处理，批内也会互相去重。"""
    index = DuplicateIndex()
    for fp in existing:
        index.add(fp)
    dups: dict[int, int] = {}
    for fp in sorted(new, key=lambda f: f.item_id):
        original = index.find(fp)
        if original is not None and original < fp.item_id:
            dups[fp.item_id] = original
        else:
            index.add(fp)
    return dups
