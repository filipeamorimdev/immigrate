"""Read an Apple Photos library without macOS.

The library is treated as read-only. osxphotos copies the database to a temp
folder before opening it, so nothing is written to the mounted library.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import timezones

logging.getLogger("osxphotos").setLevel(logging.ERROR)


@dataclass
class Part:
    role: str  # main, live, raw or edit
    path: str
    filename: str
    size: int


@dataclass
class Item:
    uuid: str
    filename: str
    taken: datetime
    modified: datetime
    favorite: bool = False
    hidden: bool = False
    is_video: bool = False
    in_icloud: bool = False
    referenced: bool = False
    expected_path: str = ""
    title: str = ""
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    latitude: float | None = None
    longitude: float | None = None
    albums: list[str] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)

    @property
    def missing(self) -> bool:
        return not any(p.role == "main" for p in self.parts)

    @property
    def missing_reason(self) -> str | None:
        if not self.missing:
            return None
        if self.referenced:
            return "referenced"
        return "icloud" if self.in_icloud else "not_found"

    def part(self, role: str) -> Part | None:
        return next((p for p in self.parts if p.role == role), None)

    def size_for(self, include_edits: bool) -> int:
        return sum(p.size for p in self.parts if include_edits or p.role != "edit")


@dataclass
class Scan:
    library: str
    photos_version: str
    items: list[Item]
    album_names: list[str]
    people_count: int
    scanned_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for item in data["items"]:
            item["taken"] = item["taken"].isoformat()
            item["modified"] = item["modified"].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Scan:
        items = [
            Item(
                **{
                    **item,
                    "taken": datetime.fromisoformat(item["taken"]),
                    "modified": datetime.fromisoformat(item["modified"]),
                    "parts": [Part(**part) for part in item["parts"]],
                }
            )
            for item in data["items"]
        ]
        return cls(**{**data, "items": items})

    def summary(self) -> dict[str, Any]:
        items = self.items
        return {
            "library": self.library,
            "photosVersion": self.photos_version,
            "scannedAt": self.scanned_at,
            "total": len(items),
            "photos": sum(1 for i in items if not i.is_video),
            "videos": sum(1 for i in items if i.is_video),
            "live": sum(1 for i in items if i.part("live")),
            "raw": sum(1 for i in items if i.part("raw")),
            "edited": sum(1 for i in items if i.part("edit")),
            "favorites": sum(1 for i in items if i.favorite),
            "hidden": sum(1 for i in items if i.hidden),
            "missing": sum(1 for i in items if i.missing),
            "missingInIcloud": sum(1 for i in items if i.missing_reason == "icloud"),
            "missingReferenced": sum(1 for i in items if i.missing_reason == "referenced"),
            "albums": len(self.album_names),
            "people": self.people_count,
            "bytesOriginals": sum(i.size_for(False) for i in items),
            "bytesWithEdits": sum(i.size_for(True) for i in items),
        }

    def missing_items(self) -> list[dict[str, Any]]:
        prefix = self.library.rstrip("/") + "/"
        return [
            {
                "filename": i.filename,
                "taken": i.taken.isoformat(),
                "reason": i.missing_reason,
                "path": i.expected_path.removeprefix(prefix),
            }
            for i in self.items
            if i.missing
        ]


def _existing(path: str | None) -> tuple[str, int] | None:
    if not path:
        return None
    try:
        return path, os.stat(path).st_size
    except OSError:
        return None


def _album_title(album: Any) -> str:
    folders = [f for f in (getattr(album, "folder_names", None) or []) if f]
    return "/".join([*folders, album.title])


def _clean_people(names: Iterable[str] | None) -> list[str]:
    return sorted({n for n in (names or []) if n and n != "_UNKNOWN_"})


def _to_item(photo: Any) -> Item:
    original_name = photo.original_filename or photo.filename
    stem, ext = os.path.splitext(original_name)
    latitude, longitude = photo.location if photo.location else (None, None)
    item = Item(
        uuid=photo.uuid,
        filename=original_name,
        taken=photo.date,
        modified=photo.date_modified or photo.date,
        favorite=bool(photo.favorite),
        hidden=bool(photo.hidden),
        is_video=bool(photo.ismovie),
        in_icloud=bool(photo.incloud or photo.iscloudasset),
        referenced=bool(photo.isreference),
        title=photo.title or "",
        description=photo.description or "",
        keywords=sorted(set(photo.keywords or [])),
        people=_clean_people(photo.persons),
        latitude=latitude,
        longitude=longitude,
        albums=sorted({_album_title(a) for a in photo.album_info}),
    )

    seen: set[str] = set()

    def add(role: str, path: str | None, filename: str) -> None:
        found = _existing(path)
        if found and found[0] not in seen:
            seen.add(found[0])
            item.parts.append(Part(role, found[0], filename, found[1]))

    add("main", photo.path, original_name)
    if photo.live_photo:
        add("live", photo.path_live_photo, f"{stem}.mov")
    if photo.has_raw:
        raw_path = photo.path_raw
        raw_ext = Path(raw_path).suffix if raw_path else ""
        add("raw", raw_path, f"{stem}{raw_ext}")
    if photo.hasadjustments:
        edited = photo.path_edited
        edited_ext = Path(edited).suffix if edited else ext
        add("edit", edited, f"{stem}_edited{edited_ext}")

    # A photo whose original is gone but whose edit survives is still worth keeping.
    if item.missing and (edit := item.part("edit")):
        edit.role = "main"
    if item.missing:
        item.expected_path = _expected_path(photo)
    return item


def _expected_path(photo: Any) -> str:
    # osxphotos only gives the original's path once the file exists, so ask for where it should be.
    try:
        return str(photo._path_5() or "")
    except (AttributeError, KeyError):
        return ""


def scan_library(library: str, progress: Callable[[int, int], None] | None = None) -> Scan:
    import osxphotos

    db = osxphotos.PhotosDB(dbfile=library, verbose=lambda *_: None)
    try:
        photos = db.photos(intrash=False)
        items: list[Item] = []
        for index, photo in enumerate(photos, 1):
            items.append(_to_item(photo))
            if progress and (index % 50 == 0 or index == len(photos)):
                progress(index, len(photos))
        photos_version = str(db.photos_version)
    finally:
        # Otherwise the database copy stays in TMPDIR until garbage collection gets to it.
        db._tempdir.cleanup()
    items.sort(key=lambda i: (i.taken.timestamp(), i.uuid))
    albums = sorted({name for item in items for name in item.albums})
    people = {name for item in items for name in item.people}
    return Scan(
        library=library,
        photos_version=photos_version,
        items=items,
        album_names=albums,
        people_count=len(people),
        scanned_at=timezones.now().isoformat(timespec="seconds"),
    )
