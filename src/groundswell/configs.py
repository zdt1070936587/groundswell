"""configs/*.yaml 的 schema 与加载。"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from groundswell.config import get_settings

GLOBAL = "GLOBAL"


# ---------------------------------------------------------------- regions.yaml


class CountryConfig(BaseModel):
    name: str
    languages: list[str] = []
    timezone: str | None = None
    aliases: list[str] = []


class RegionContext(BaseModel):
    channels: list[str] = []
    payments: list[str] = []
    regulation: list[str] = []
    notes: str | None = None


class RegionConfig(BaseModel):
    name: str
    timezone: str = "UTC"
    countries: dict[str, CountryConfig] = {}
    context: RegionContext = RegionContext()

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v


class RegionsFile(BaseModel):
    regions: dict[str, RegionConfig]

    @model_validator(mode="after")
    def _check(self) -> RegionsFile:
        if GLOBAL not in self.regions:
            raise ValueError("regions.yaml 必须包含 GLOBAL")
        seen: dict[str, str] = {}
        for code, region in self.regions.items():
            for cc, country in region.countries.items():
                if cc in seen:
                    raise ValueError(f"国家 {cc} 同时属于 {seen[cc]} 与 {code}")
                seen[cc] = code
                if country.timezone:
                    ZoneInfo(country.timezone)
        return self

    def region_of_country(self, country: str | None) -> str | None:
        if not country:
            return None
        country = country.upper()
        for code, region in self.regions.items():
            if country in region.countries:
                return code
        return None

    def timezone_of(self, region: str) -> ZoneInfo:
        cfg = self.regions.get(region)
        return ZoneInfo(cfg.timezone if cfg else "UTC")

    def primary_country(self, region: str) -> str | None:
        cfg = self.regions.get(region)
        if not cfg or not cfg.countries:
            return None
        return next(iter(cfg.countries))

    @property
    def codes(self) -> list[str]:
        return list(self.regions)


# ---------------------------------------------------------------- sources.yaml


class ChannelConfig(BaseModel):
    region: str | None = None
    country: str | None = None
    language: str | None = None


class SourceConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$", max_length=64)
    kind: str
    name: str
    region: str = GLOBAL
    country: str | None = None
    language: str | None = None
    interval_minutes: int = Field(default=60, ge=5)
    enabled: bool = True
    category_hint: str | None = None
    params: dict[str, Any] = {}
    channels: dict[str, ChannelConfig] = {}

    def channel(self, name: str | None) -> ChannelConfig | None:
        if name is None:
            return None
        return self.channels.get(name) or self.channels.get(name.lower())


class SourcesFile(BaseModel):
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def _unique(self) -> SourcesFile:
        ids = [s.id for s in self.sources]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"重复的 source id: {sorted(dup)}")
        return self

    def get(self, source_id: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.id == source_id), None)


# ---------------------------------------------------------------- methodology.yaml


class Principle(BaseModel):
    id: str
    person: str
    principle: str
    question: str
    tag: str
    weight: float = 1.0
    region_specific: bool = False
    signals: list[str] = []


class MethodologyFile(BaseModel):
    version: str
    principles: list[Principle]

    @model_validator(mode="after")
    def _unique(self) -> MethodologyFile:
        ids = [p.id for p in self.principles]
        if len(ids) != len(set(ids)):
            raise ValueError("methodology.yaml 中存在重复 id")
        return self

    @property
    def tags(self) -> list[str]:
        return sorted({p.tag for p in self.principles})


# ---------------------------------------------------------------- calendars.yaml


class CalendarEvent(BaseModel):
    name: str
    regions: list[str]
    # 每年固定：MM-DD；或按年份给出具体日期（农历节日、赛事）
    start: str | None = None
    end: str | None = None
    dates: dict[int, tuple[date, date]] = {}
    factor: float = Field(default=0.7, gt=0, le=1)

    def active_on(self, day: date) -> bool:
        if day.year in self.dates:
            s, e = self.dates[day.year]
            return s <= day <= e
        if self.start and self.end:
            key = day.strftime("%m-%d")
            if self.start <= self.end:
                return self.start <= key <= self.end
            return key >= self.start or key <= self.end
        return False


class CalendarsFile(BaseModel):
    events: list[CalendarEvent] = []


# ---------------------------------------------------------------- slow_variables.yaml / golden_set.yaml


class SlowVariable(BaseModel):
    id: str
    name: str
    description: str
    direction: str = "up"
    keywords: list[str]


class SlowVariablesFile(BaseModel):
    variables: list[SlowVariable] = []


class GoldenCase(BaseModel):
    name: str
    keywords: list[str]
    region: str = GLOBAL
    breakout: date
    approx: bool = True
    note: str | None = None


class GoldenSetFile(BaseModel):
    version: str
    cases: list[GoldenCase]


# ---------------------------------------------------------------- 加载


def _config_dir(config_dir: Path | None = None) -> Path:
    return Path(config_dir or get_settings().config_dir)


def _load(name: str, model: type[BaseModel], config_dir: Path | None = None) -> Any:
    path = _config_dir(config_dir) / name
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return model.model_validate(data)


@lru_cache
def load_regions(config_dir: Path | None = None) -> RegionsFile:
    return _load("regions.yaml", RegionsFile, config_dir)


@lru_cache
def load_sources(config_dir: Path | None = None) -> SourcesFile:
    return _load("sources.yaml", SourcesFile, config_dir)


@lru_cache
def load_methodology(config_dir: Path | None = None) -> MethodologyFile:
    return _load("methodology.yaml", MethodologyFile, config_dir)


@lru_cache
def load_calendars(config_dir: Path | None = None) -> CalendarsFile:
    return _load("calendars.yaml", CalendarsFile, config_dir)


@lru_cache
def load_slow_variables(config_dir: Path | None = None) -> SlowVariablesFile:
    return _load("slow_variables.yaml", SlowVariablesFile, config_dir)


def load_golden_set(path: Path | None = None) -> GoldenSetFile:
    path = path or _config_dir() / "golden_set.yaml"
    with Path(path).open(encoding="utf-8") as fh:
        return GoldenSetFile.model_validate(yaml.safe_load(fh))


def clear_caches() -> None:
    for fn in (load_regions, load_sources, load_methodology, load_calendars, load_slow_variables):
        fn.cache_clear()


def check_all(config_dir: Path | None = None) -> list[str]:
    """交叉校验全部配置，返回问题列表（空列表表示通过）。"""
    from groundswell.collectors.registry import COLLECTORS, load_builtin_collectors

    load_builtin_collectors()
    problems: list[str] = []
    try:
        regions = load_regions(config_dir)
    except Exception as exc:  # noqa: BLE001
        return [f"regions.yaml: {exc}"]

    try:
        sources = load_sources(config_dir)
    except Exception as exc:  # noqa: BLE001
        return [f"sources.yaml: {exc}"]

    for src in sources.sources:
        if src.kind not in COLLECTORS:
            problems.append(f"source {src.id}: 未知采集器类型 {src.kind}")
        if src.region not in regions.regions:
            problems.append(f"source {src.id}: 未知区域 {src.region}")
        if src.country:
            owner = regions.region_of_country(src.country)
            if owner is None:
                problems.append(f"source {src.id}: 国家 {src.country} 未在 regions.yaml 中声明")
            elif owner != src.region:
                problems.append(
                    f"source {src.id}: 国家 {src.country} 属于 {owner}，与 region={src.region} 不一致"
                )
        for name, ch in src.channels.items():
            if ch.region and ch.region not in regions.regions:
                problems.append(f"source {src.id} channel {name}: 未知区域 {ch.region}")
            if ch.country and regions.region_of_country(ch.country) is None:
                problems.append(f"source {src.id} channel {name}: 国家 {ch.country} 未声明")

    for name, loader in (
        ("methodology.yaml", load_methodology),
        ("calendars.yaml", load_calendars),
        ("slow_variables.yaml", load_slow_variables),
    ):
        try:
            data = loader(config_dir)
        except FileNotFoundError:
            problems.append(f"{name}: 文件不存在")
            continue
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{name}: {exc}")
            continue
        if isinstance(data, CalendarsFile):
            for ev in data.events:
                bad = [r for r in ev.regions if r not in regions.regions]
                if bad:
                    problems.append(f"calendars.yaml {ev.name}: 未知区域 {bad}")
                if not ev.dates and not (ev.start and ev.end):
                    problems.append(f"calendars.yaml {ev.name}: 需要 start/end 或 dates")

    golden = _config_dir(config_dir) / "golden_set.yaml"
    if golden.exists():
        try:
            gs = load_golden_set(golden)
            for case in gs.cases:
                if case.region not in regions.regions:
                    problems.append(f"golden_set.yaml {case.name}: 未知区域 {case.region}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"golden_set.yaml: {exc}")
    return problems
