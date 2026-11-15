"""What a check found, in the shape the page shows it: copies, what Immich has, and gaps."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from . import timezones
from .check import Check
from .folders import FOLDER
from .library import Item, Scan
from .state import State

LIST_LIMIT = 500
# A location is only suggested from a photo taken within this many seconds.
SUGGEST_WITHIN = 24 * 60 * 60


def _moment(text: str | None) -> float | None:
    """Seconds since 1970, for ordering. A date without a time zone is taken as UTC."""
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp()


def _group(item: Item, kind: str) -> str:
    """Folders are grouped by the folder a file is in, and Photos libraries by day."""
    if kind == FOLDER:
        return item.albums[0] if item.albums else ""
    return item.taken.astimezone(timezones.current()).date().isoformat()


def _where(item: Item, root: str) -> str:
    """Where to find the item: its path in a folder, or its name in a Photos library."""
    main = item.part("main")
    if main is None or not item.from_folder:
        return item.filename
    path = os.path.relpath(main.path, root) if main.path.startswith(root) else main.filename
    return path.encode("utf-8", "replace").decode()


def findings(scan: Scan, check: Check, state: State, kind: str, root: str) -> dict[str, Any]:
    by_uuid = {item.uuid: item for item in scan.items}
    records = state.all_items()
    fixes = state.fixes()

    def asset_of(item: Item) -> str | None:
        record = records.get(item.uuid)
        if record and record["status"] == "done" and record["primary_id"]:
            return str(record["primary_id"])
        return check.existing_asset(item)

    groups: dict[str, list[str]] = {}
    for copy, keeper in check.copies.items():
        groups.setdefault(keeper, []).append(copy)
    copies = [
        {
            "keeper": _where(by_uuid[keeper], root),
            "copies": [_where(by_uuid[uuid], root) for uuid in uuids if uuid in by_uuid],
        }
        for keeper, uuids in list(groups.items())[:LIST_LIMIT]
        if keeper in by_uuid
    ]

    trashed = set(check.trashed)
    existing = []
    existing_items: list[Item] = []
    for item in scan.items:
        if item.uuid in check.copies or item.missing:
            continue
        asset = check.existing_asset(item)
        record = records.get(item.uuid)
        if asset and not (record and record["status"] == "done"):
            existing_items.append(item)
            main = item.part("main")
            existing.append(
                {
                    "filename": _where(item, root),
                    "assetId": asset,
                    "trashed": bool(main and check.checksum(main.path) in trashed),
                }
            )

    # When each item was taken, as well as is known, and where, for suggestions.
    known_times: dict[str, float | None] = {}
    known_places: dict[str, list[float]] = dict(check.places)
    for uuid, fix in fixes.items():
        if fix["latitude"] is not None and fix["longitude"] is not None:
            known_places[uuid] = [fix["latitude"], fix["longitude"]]
    for item in scan.items:
        # A folder's file without a capture date only has the time it was copied.
        found = check.dates.get(item.uuid) if kind == FOLDER else item.taken.isoformat()
        known_times[item.uuid] = _moment(found)

    placed: dict[str, list[Item]] = {}
    for uuid in known_places:
        if (holder := by_uuid.get(uuid)) is not None:
            placed.setdefault(_group(holder, kind), []).append(holder)

    def suggestion(item: Item) -> dict[str, Any] | None:
        when = known_times[item.uuid]
        if when is None:
            return None
        candidates = [
            (abs(other_time - when), other)
            for other in placed.get(_group(item, kind), [])
            if other is not item and (other_time := known_times[other.uuid]) is not None
        ]
        if not candidates:
            return None
        apart, nearest = min(candidates, key=lambda pair: pair[0])
        if apart > SUGGEST_WITHIN:
            return None
        latitude, longitude = known_places[nearest.uuid]
        return {
            "latitude": latitude,
            "longitude": longitude,
            "from": nearest.filename,
            "minutes": round(apart / 60),
        }

    missing_date, missing_location = set(check.missing_date), set(check.missing_location)
    gaps = []
    gap_items: list[Item] = []
    for item in scan.items:
        if item.uuid not in missing_date and item.uuid not in missing_location:
            continue
        gap_items.append(item)
        chosen = fixes.get(item.uuid)
        gaps.append(
            {
                "uuid": item.uuid,
                "filename": item.filename,
                "where": _where(item, root),
                "group": _group(item, kind),
                # What Immich uses without a fix: the capture date, or else the file's time.
                "date": check.dates.get(item.uuid) or item.taken.isoformat(),
                "missingDate": item.uuid in missing_date,
                "missingLocation": item.uuid in missing_location,
                "fix": {k: v for k, v in chosen.items() if k != "applied"} if chosen else None,
                "applied": bool(chosen and chosen["applied"]),
                "assetId": asset_of(item),
                "suggestion": suggestion(item) if item.uuid in missing_location else None,
            }
        )

    def split(items: list[Item]) -> dict[str, int]:
        return {
            "photos": sum(1 for i in items if not i.is_video),
            "videos": sum(1 for i in items if i.is_video),
        }

    return {
        "split": {
            "copies": split([by_uuid[u] for u in check.copies if u in by_uuid]),
            "existing": split(existing_items),
            "date": split([i for i in gap_items if i.uuid in missing_date]),
            "location": split([i for i in gap_items if i.uuid in missing_location]),
        },
        "copies": copies,
        "copyGroups": len(groups),
        "existing": existing[:LIST_LIMIT],
        "existingCount": len(existing),
        "gaps": gaps,
    }
