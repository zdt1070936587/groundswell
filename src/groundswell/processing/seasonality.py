"""区域日历：节假日、大促、赛事期间对突发分量打折，避免季节性峰值被当成趋势。"""

from __future__ import annotations

from datetime import datetime

from groundswell.configs import CalendarsFile, RegionsFile


def seasonal_factor(
    region: str, hour: datetime, calendars: CalendarsFile, regions: RegionsFile
) -> float:
    local_day = hour.astimezone(regions.timezone_of(region)).date()
    factor = 1.0
    for event in calendars.events:
        if region in event.regions and event.active_on(local_day):
            factor = min(factor, event.factor)
    return factor


def active_events(
    region: str, hour: datetime, calendars: CalendarsFile, regions: RegionsFile
) -> list[str]:
    local_day = hour.astimezone(regions.timezone_of(region)).date()
    return [e.name for e in calendars.events if region in e.regions and e.active_on(local_day)]
