"""The time zone that the log and the page show times in. It's chosen in Settings."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

DEFAULT = "UTC"
# The regions of the tz database. Older aliases such as US/Eastern are left out of the list.
REGIONS = (
    "Africa",
    "America",
    "Antarctica",
    "Arctic",
    "Asia",
    "Atlantic",
    "Australia",
    "Europe",
    "Indian",
    "Pacific",
)

_zone = ZoneInfo(DEFAULT)


@cache
def names() -> tuple[str, ...]:
    zones = (name for name in available_timezones() if name.split("/")[0] in REGIONS)
    return (DEFAULT, *sorted(zones))


def is_valid(name: object) -> bool:
    return isinstance(name, str) and name in names()


def initial(saved: object) -> str:
    """The saved time zone, or else TZ from the environment as earlier versions used it."""
    for candidate in (saved, os.environ.get("TZ")):
        if is_valid(candidate):
            return str(candidate)
    return DEFAULT


def use(name: str) -> None:
    global _zone
    _zone = ZoneInfo(name)


def now() -> datetime:
    return datetime.now(_zone)


class Formatter(logging.Formatter):
    """Writes log times in the chosen time zone, whatever the container's own one is."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, _zone)
        if datefmt:
            return moment.strftime(datefmt)
        return f"{moment:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"
