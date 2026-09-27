from datetime import date

import pytest
from pydantic import ValidationError

from groundswell.configs import (
    CalendarEvent,
    RegionsFile,
    SourcesFile,
    check_all,
    load_calendars,
    load_regions,
    load_sources,
)


def test_repo_configs_pass_cross_validation():
    assert check_all() == []


def test_region_lookup():
    regions = load_regions()
    assert regions.region_of_country("cn") == "GC"
    assert regions.region_of_country("BR") == "LATAM"
    assert regions.region_of_country("ZZ") is None
    assert regions.timezone_of("GC").key == "Asia/Shanghai"
    assert "GLOBAL" in regions.codes


def test_country_cannot_belong_to_two_regions():
    with pytest.raises(ValidationError):
        RegionsFile.model_validate(
            {
                "regions": {
                    "GLOBAL": {"name": "g"},
                    "A": {"name": "a", "countries": {"SG": {"name": "sg"}}},
                    "B": {"name": "b", "countries": {"SG": {"name": "sg"}}},
                }
            }
        )


def test_duplicate_source_ids_rejected():
    src = {"id": "x", "kind": "rss", "name": "x"}
    with pytest.raises(ValidationError):
        SourcesFile.model_validate({"sources": [src, src]})


def test_every_enabled_source_has_known_region():
    regions = load_regions()
    for src in load_sources().sources:
        assert src.region in regions.regions


def test_calendar_fixed_and_year_specific_dates():
    wrap = CalendarEvent(name="xmas", regions=["NA"], start="12-20", end="01-02")
    assert wrap.active_on(date(2026, 12, 25))
    assert wrap.active_on(date(2027, 1, 1))
    assert not wrap.active_on(date(2027, 1, 5))
    cny = next(e for e in load_calendars().events if e.name == "春节")
    assert cny.active_on(date(2026, 2, 17))
    assert not cny.active_on(date(2026, 3, 17))
