"""Read capture dates and locations from files with exiftool, the tool Immich uses too.

exiftool only reads here. It's given no option that writes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

# Immich v3.3.5 looks for a date in these tags, in this order (EXIF_DATE_TAGS in
# server/src/services/metadata.service.ts). Using the same list means "no date" here
# is "no date" in Immich.
DATE_TAGS = (
    "SubSecDateTimeOriginal",
    "SubSecCreateDate",
    "DateTimeOriginal",
    "CreationDate",
    "CreateDate",
    "MediaCreateDate",
    "DateTimeCreated",
    "GPSDateTime",
    "DateTimeUTC",
    "SonyDateTime2",
    "SourceImageCreateTime",
)
GPS_TAGS = (
    "GPSLatitude",
    "GPSLongitude",
    "GPSLatitudeRef",
    "GPSLongitudeRef",
    "GPSCoordinates",
)
# Where a signed location comes from, best first. EXIF keeps the sign in a separate
# reference tag, so its plain value is only used together with that.
GPS_GROUPS = ("Composite", "XMP", "QuickTime", "Keys", "UserData", "ItemList")
BATCH = 200
TIMEOUT_SECONDS = 900
_DATE = re.compile(
    r"^(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d)(?::(\d\d))?(\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?"
)


def available() -> bool:
    return shutil.which("exiftool") is not None


def parse_date(value: Any) -> str | None:
    """Turn an EXIF date such as 2019:03:14 10:00:00+01:00 into ISO form, or None."""
    if not isinstance(value, str):
        return None
    match = _DATE.match(value.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    if year == "0000" or month == "00" or day == "00":
        return None
    if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31 and int(hour) < 24):
        return None
    text = f"{year}-{month}-{day}T{hour}:{minute}:{second or '00'}{fraction or ''}"
    if zone:
        text += "+00:00" if zone == "Z" else f"{zone[:3]}:{zone[-2:]}"
    return text


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN isn't a number here


def _by_tag(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Group exiftool's "Group:Tag" keys by tag name."""
    tags: dict[str, dict[str, Any]] = {}
    for key, value in record.items():
        group, _, name = key.rpartition(":")
        tags.setdefault(name, {})[group] = value
    return tags


def _signed(tags: dict[str, dict[str, Any]], name: str, negative: str) -> float | None:
    values = tags.get(name, {})
    for group in GPS_GROUPS:
        if (number := _number(values.get(group))) is not None:
            return number
    number = _number(values.get("EXIF"))
    if number is None:
        return None
    reference = str(tags.get(f"{name}Ref", {}).get("EXIF", "")).upper()
    return -abs(number) if reference.startswith(negative) else number


def parse_record(record: dict[str, Any]) -> dict[str, Any]:
    """Pick the date and location out of one file's exiftool output, read with -G -n."""
    tags = _by_tag(record)
    date = next(
        (
            d
            for tag in DATE_TAGS
            for value in tags.get(tag, {}).values()
            if (d := parse_date(value))
        ),
        None,
    )
    latitude = _signed(tags, "GPSLatitude", "S")
    longitude = _signed(tags, "GPSLongitude", "W")
    if latitude is None or longitude is None:
        for value in tags.get("GPSCoordinates", {}).values():
            parts = str(value).split()
            if len(parts) >= 2:
                latitude, longitude = _number(parts[0]), _number(parts[1])
                break
    if (
        latitude is None
        or longitude is None
        or not (-90 <= latitude <= 90 and -180 <= longitude <= 180)
        # Immich treats 0, 0 as no location, since cameras write it when they have none.
        or (latitude == 0 and longitude == 0)
    ):
        latitude = longitude = None
    return {"date": date, "latitude": latitude, "longitude": longitude}


def _run(paths: list[str]) -> dict[str, dict[str, Any]]:
    arguments = [
        "exiftool",
        "-json",
        "-n",
        "-G",
        "-a",
        "-quiet",
        "-quiet",
        "-charset",
        "filename=utf8",
        *(f"-{tag}" for tag in (*DATE_TAGS, *GPS_TAGS)),
        "-@",
        "-",
    ]
    names = b"".join(os.fsencode(path) + b"\n" for path in paths)
    result = subprocess.run(  # noqa: S603 - fixed program, paths go in through stdin
        arguments, input=names, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
    )
    if not result.stdout.strip():
        return {}
    records = json.loads(result.stdout.decode("utf-8", "replace"))
    return {
        str(record.get("SourceFile")): parse_record(record)
        for record in records
        # A file exiftool couldn't make sense of isn't known to lack a date.
        if not any(key.endswith("Error") for key in record)
    }


def read(paths: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Return the date and location found in each file that exiftool could read."""
    # A name with a line break can't go through exiftool's list of files.
    wanted = [path for path in paths if "\n" not in path and "\r" not in path]
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), BATCH):
        batch = wanted[start : start + BATCH]
        try:
            results = _run(batch)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.warning("exiftool couldn't read %d files: %s", len(batch), exc)
            continue
        for path in batch:
            if path in results:
                found[path] = results[path]
    return found
