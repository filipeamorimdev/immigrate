"""Check a library after it's read: duplicates, what Immich already has, and missing details.

Every file is read once to get its checksum, and a folder's files are also read for
their date and location. What was learned is kept in state.db, so later checks only
read files that changed. Nothing here writes to the library.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from . import metadata, timezones
from .immich import Immich
from .library import Item, Scan
from .state import State

log = logging.getLogger(__name__)

HASH_WORKERS = 3
SAVE_EVERY = 200
CHUNK = 1024 * 1024


def file_key(path: str) -> str:
    # A path can hold bytes that SQLite can't store as text, so it's stored as a hash.
    return hashlib.sha1(os.fsencode(path), usedforsecurity=False).hexdigest()


def sha1_of(path: str, stop: threading.Event | None = None) -> str | None:
    digest = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as file:
        while chunk := file.read(CHUNK):
            if stop is not None and stop.is_set():
                return None
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Check:
    library: str
    scanned_at: str  # the read this check belongs to
    checked_at: str = ""
    complete: bool = False
    details_read: bool = False  # exiftool read the folder's dates and locations
    immich_asked: bool = False
    files: dict[str, list[Any]] = field(default_factory=dict)  # path: [size, mtime_ns, sha1]
    copies: dict[str, str] = field(default_factory=dict)  # uuid: the uuid it's a copy of
    in_immich: dict[str, str] = field(default_factory=dict)  # sha1: asset ID
    trashed: list[str] = field(default_factory=list)  # sha1s whose asset is in Immich's trash
    dates: dict[str, str] = field(default_factory=dict)  # uuid: capture date found in the file
    places: dict[str, list[float]] = field(default_factory=dict)  # uuid: [latitude, longitude]
    missing_date: list[str] = field(default_factory=list)
    missing_location: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)  # files that couldn't be read

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Check:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def checksum(self, path: str) -> str | None:
        found = self.files.get(path)
        return found[2] if found else None

    def still_matches(self, path: str) -> bool:
        """Whether the file is still the one that was checked."""
        found = self.files.get(path)
        if not found:
            return False
        try:
            stat = os.stat(path)
        except OSError:
            return False
        return bool(found[0] == stat.st_size and found[1] == stat.st_mtime_ns)

    def existing_asset(self, item: Item) -> str | None:
        """The Immich asset that already holds this item's original, if any."""
        main = item.part("main")
        checksum = self.checksum(main.path) if main else None
        return self.in_immich.get(checksum) if checksum else None

    def summary(self) -> dict[str, Any]:
        return {
            "checkedAt": self.checked_at,
            "complete": self.complete,
            "detailsRead": self.details_read,
            "immichAsked": self.immich_asked,
            "copies": len(self.copies),
            "inImmich": len(self.in_immich),
            "trashed": len(self.trashed),
            "missingDate": len(self.missing_date),
            "missingLocation": len(self.missing_location),
            "unreadable": len(self.unreadable),
            "unreadableFiles": self.unreadable[:20],
        }


class Checker:
    def __init__(
        self,
        scan: Scan,
        state: State,
        immich: Immich | None,
        is_folder: bool,
        stop: threading.Event,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.scan, self.state, self.immich, self.is_folder = scan, state, immich, is_folder
        self.stop = stop
        self.progress = progress or (lambda *_: None)
        self.result = Check(library=scan.library, scanned_at=scan.scanned_at)
        self.lock = threading.Lock()

    def run(self) -> Check:
        items = [item for item in self.scan.items if not item.missing]
        cache = self.state.files()
        stats = self.stat_files(items)
        self.hash_files(stats, cache)
        if self.stop.is_set():
            return self.finish()
        if self.is_folder:
            self.read_details(items, stats, cache)
        if self.stop.is_set():
            return self.finish()
        self.find_copies(items)
        self.find_gaps(items, cache)
        if self.immich is not None and not self.stop.is_set():
            self.ask_immich()
        self.result.complete = not self.stop.is_set()
        return self.finish()

    def finish(self) -> Check:
        self.result.checked_at = timezones.now().isoformat(timespec="seconds")
        return self.result

    def stat_files(self, items: list[Item]) -> dict[str, tuple[int, int]]:
        stats: dict[str, tuple[int, int]] = {}
        paths = [part.path for item in items for part in item.parts]
        paths += [item.sidecar_path for item in items if item.sidecar_path]
        for path in dict.fromkeys(paths):
            try:
                stat = os.stat(path)
            except OSError:
                self.result.unreadable.append(os.path.basename(path))
                continue
            stats[path] = (stat.st_size, stat.st_mtime_ns)
        return stats

    def hash_files(
        self, stats: dict[str, tuple[int, int]], cache: dict[str, tuple[int, int, str | None, Any]]
    ) -> None:
        todo: list[str] = []
        for path, (size, mtime) in stats.items():
            cached = cache.get(file_key(path))
            if cached and cached[0] == size and cached[1] == mtime and cached[2]:
                self.result.files[path] = [size, mtime, cached[2]]
            else:
                todo.append(path)
        total = sum(stats[path][0] for path in todo)
        done = 0
        pending: list[tuple[str, int, int, str | None, Any]] = []
        self.progress("checksums", 0, total)

        def work(path: str) -> None:
            nonlocal done
            size, mtime = stats[path]
            try:
                checksum = sha1_of(path, self.stop)
            except OSError:
                with self.lock:
                    self.result.unreadable.append(os.path.basename(path))
                return
            if checksum is None:
                return
            with self.lock:
                self.result.files[path] = [size, mtime, checksum]
                # Details already learned for this exact file stay with it.
                cached = cache.get(file_key(path))
                details = cached[3] if cached and cached[:2] == (size, mtime) else None
                pending.append((file_key(path), size, mtime, checksum, details))
                done += size
                if len(pending) >= SAVE_EVERY:
                    self.state.save_files(pending[:])
                    pending.clear()
                self.progress("checksums", done, total)

        with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
            for future in [pool.submit(work, path) for path in todo]:
                future.result()
        self.state.save_files(pending)

    def read_details(
        self,
        items: list[Item],
        stats: dict[str, tuple[int, int]],
        cache: dict[str, tuple[int, int, str | None, Any]],
    ) -> None:
        if not metadata.available():
            log.warning("exiftool isn't installed, so dates and locations weren't checked")
            return
        paths = [p for item in items for p in (item.parts[0].path, item.sidecar_path) if p]
        todo = []
        for path in dict.fromkeys(paths):
            cached = cache.get(file_key(path))
            if path in stats and not (cached and cached[:2] == stats[path] and cached[3]):
                todo.append(path)
        self.progress("details", 0, len(todo))
        for start in range(0, len(todo), metadata.BATCH):
            if self.stop.is_set():
                return
            batch = todo[start : start + metadata.BATCH]
            found = metadata.read(batch)
            rows = []
            for path, details in found.items():
                size, mtime = stats[path]
                checksum = self.result.checksum(path)
                rows.append((file_key(path), size, mtime, checksum, details))
                cache[file_key(path)] = (size, mtime, checksum, details)
            self.state.save_files(rows)
            self.progress("details", min(start + len(batch), len(todo)), len(todo))
        self.result.details_read = True

    def find_copies(self, items: list[Item]) -> None:
        groups: dict[str, list[Item]] = {}
        for item in items:
            main = item.part("main")
            checksum = self.result.checksum(main.path) if main else None
            if checksum:
                groups.setdefault(checksum, []).append(item)
        for group in groups.values():
            if len(group) < 2:
                continue
            # Keep the copy that carries the most, so less is lost when copies are left out.
            keeper = max(
                group,
                key=lambda i: (bool(i.sidecar_path), i.latitude is not None, bool(i.albums)),
            )
            for item in group:
                if item is not keeper:
                    self.result.copies[item.uuid] = keeper.uuid

    def find_gaps(
        self, items: list[Item], cache: dict[str, tuple[int, int, str | None, Any]]
    ) -> None:
        for item in items:
            if item.uuid in self.result.copies:
                continue
            if not self.is_folder:
                # Photos gives every item a date, so only a location can be missing.
                if item.latitude is not None and item.longitude is not None:
                    self.result.places[item.uuid] = [item.latitude, item.longitude]
                else:
                    self.result.missing_location.append(item.uuid)
                continue
            if not self.result.details_read:
                continue
            found = [
                cached[3]
                for path in (item.sidecar_path, item.parts[0].path)
                if path and (cached := cache.get(file_key(path))) and cached[3]
            ]
            if not found:
                continue  # exiftool couldn't read it, so nothing is known either way
            # Like Immich, a sidecar's details come before the file's own.
            date = next((d["date"] for d in found if d["date"]), None)
            place = next(
                ([d["latitude"], d["longitude"]] for d in found if d["latitude"] is not None),
                None,
            )
            if date:
                self.result.dates[item.uuid] = date
            else:
                self.result.missing_date.append(item.uuid)
            if place:
                self.result.places[item.uuid] = place
            else:
                self.result.missing_location.append(item.uuid)

    def ask_immich(self) -> None:
        if self.immich is None:
            return
        checksums = sorted({entry[2] for entry in self.result.files.values() if entry[2]})
        self.progress("immich", 0, len(checksums))
        found = self.immich.existing(checksums)
        self.result.in_immich = {checksum: asset for checksum, (asset, _) in found.items()}
        self.result.trashed = sorted(checksum for checksum, (_, gone) in found.items() if gone)
        self.result.immich_asked = True
        self.progress("immich", len(checksums), len(checksums))


def run_check(
    scan: Scan,
    state: State,
    immich: Immich | None,
    is_folder: bool,
    stop: threading.Event,
    progress: Callable[[str, int, int], None] | None = None,
) -> Check:
    return Checker(scan, state, immich, is_folder, stop, progress).run()
