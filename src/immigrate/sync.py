"""One-way sync from an Apple Photos library into Immich.

Nothing here deletes anything: not in the library, not in Immich. A photo
removed from Apple Photos stays in Immich.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from .immich import Immich, ImmichError
from .library import Item, Scan
from .state import State
from .xmp import build_sidecar

log = logging.getLogger(__name__)

ALBUM_CHUNK = 500
STOP_AFTER_FAILURES_IN_A_ROW = 20

# Single-character cell codes for the mosaic on the sync screen.
WAITING, DONE, SKIPPED, FAILED, MISSING, EXCLUDED = "w", "d", "s", "f", "m", "x"


@dataclass
class Options:
    edits: str = "stack"  # "stack" keeps the edit as the cover, "original" skips edits
    albums: bool = True
    hidden: str = "skip"  # "skip" or "archive"
    people_as_tags: bool = True
    workers: int = 3

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Options:
        known = {
            key: value for key, value in (data or {}).items() if key in cls.__dataclass_fields__
        }
        options = cls(**known)
        if options.edits not in ("stack", "original"):
            raise ValueError("edits must be 'stack' or 'original'")
        if options.hidden not in ("skip", "archive"):
            raise ValueError("hidden must be 'skip' or 'archive'")
        options.workers = max(1, min(int(options.workers), 8))
        return options


def fingerprint(item: Item, options: Options) -> str:
    parts = sorted(
        (p.role, p.size) for p in item.parts if options.edits == "stack" or p.role != "edit"
    )
    source = repr(
        (
            parts,
            item.modified.isoformat(),
            item.taken.isoformat(),
            item.favorite,
            item.title,
            item.description,
            item.keywords,
            item.people if options.people_as_tags else [],
            item.latitude,
            item.longitude,
            item.hidden,
            options.hidden,
        )
    )
    return hashlib.sha1(source.encode(), usedforsecurity=False).hexdigest()


def plan(scan: Scan, state: State, options: Options) -> dict[str, Any]:
    """Work out what a sync would do, without contacting Immich."""
    known = state.all_items()
    new = changed = unchanged = excluded = missing = retry = 0
    new_bytes = 0
    for item in scan.items:
        if item.hidden and options.hidden == "skip":
            excluded += 1
        elif item.missing:
            missing += 1
        else:
            previous = known.get(item.uuid)
            current = fingerprint(item, options)
            if previous and previous["fingerprint"] == current and previous["status"] == "done":
                unchanged += 1
                continue
            if previous is None:
                new += 1
            elif previous["status"] == "failed":
                retry += 1
            else:
                changed += 1
            new_bytes += item.size_for(options.edits == "stack")
    return {
        "new": new,
        "changed": changed,
        "retry": retry,
        "unchanged": unchanged,
        "excluded": excluded,
        "missing": missing,
        "bytes": new_bytes,
        "albums": len(scan.album_names) if options.albums else 0,
        "newAlbums": sum(1 for a in scan.album_names if not state.album_id(a))
        if options.albums
        else 0,
    }


class Job:
    def __init__(self, scan: Scan, state: State, immich: Immich, options: Options):
        self.scan, self.state, self.immich, self.options = scan, state, immich, options
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.phase = "queued"
        self.message = ""
        self.error: str | None = None
        self.bytes_done = 0
        self.failures_in_a_row = 0
        self.warnings: list[str] = []
        self.index = {item.uuid: i for i, item in enumerate(scan.items)}
        self.cells = [WAITING] * len(scan.items)
        self.albums_done = 0
        self.albums_total = 0
        self.known = state.all_items()

    def mark(self, item: Item, code: str, added_bytes: int = 0) -> None:
        with self.lock:
            self.cells[self.index[item.uuid]] = code
            self.bytes_done += added_bytes

    def warn(self, text: str) -> None:
        log.warning(text)
        with self.lock:
            if len(self.warnings) < 200:
                self.warnings.append(text)

    def status(self) -> dict[str, Any]:
        with self.lock:
            cells = self.cells
            return {
                "phase": self.phase,
                "message": self.message,
                "error": self.error,
                "total": len(cells),
                "done": cells.count(DONE),
                "skipped": cells.count(SKIPPED),
                "failed": cells.count(FAILED),
                "missing": cells.count(MISSING),
                "excluded": cells.count(EXCLUDED),
                "waiting": cells.count(WAITING),
                "bytesDone": self.bytes_done,
                "albumsDone": self.albums_done,
                "albumsTotal": self.albums_total,
                "warnings": list(self.warnings[-5:]),
                "warningCount": len(self.warnings),
                "options": asdict(self.options),
            }

    def mosaic(self) -> str:
        with self.lock:
            return "".join(self.cells)

    def sync_item(self, item: Item) -> None:
        options, immich = self.options, self.immich
        current = fingerprint(item, options)
        created, modified = item.taken.isoformat(), item.modified.isoformat()
        visibility = "archive" if item.hidden else "timeline"
        sidecar = build_sidecar(item, options.people_as_tags)
        previous = self.known.get(item.uuid) or {}
        ids: dict[str, str] = dict(previous.get("asset_ids") or {})
        sent = 0

        live_id = None
        if live := item.part("live"):
            live_id = immich.upload(
                live.path, live.filename, created, modified, visibility="hidden"
            ).id
            ids["live"] = live_id
            sent += live.size

        main = item.part("main")
        if main is None:
            raise OSError(f"{item.filename} has no original on disk")
        result = immich.upload(
            main.path,
            main.filename,
            created,
            modified,
            favorite=item.favorite,
            visibility=visibility,
            live_video_id=live_id,
            sidecar=sidecar,
        )
        ids["main"] = result.id
        sent += main.size
        if result.duplicate:
            # A matching file is already in Immich, for example from the phone app.
            changes: dict[str, Any] = {}
            if item.favorite:
                changes["isFavorite"] = True
            if live_id:
                changes["livePhotoVideoId"] = live_id
            if item.hidden and options.hidden == "archive":
                changes["visibility"] = "archive"
            if changes:
                try:
                    immich.update_asset(result.id, **changes)
                except ImmichError as exc:
                    self.warn(f"{item.filename}: couldn't update the existing copy ({exc})")

        stack: list[str] = []
        if (edit := item.part("edit")) and options.edits == "stack":
            ids["edit"] = immich.upload(
                edit.path,
                edit.filename,
                created,
                modified,
                favorite=item.favorite,
                visibility=visibility,
                sidecar=sidecar,
            ).id
            sent += edit.size
            stack.append(ids["edit"])
        stack.append(ids["main"])
        if raw := item.part("raw"):
            ids["raw"] = immich.upload(
                raw.path,
                raw.filename,
                created,
                modified,
                visibility=visibility,
                sidecar=sidecar,
            ).id
            sent += raw.size
            stack.append(ids["raw"])

        if len(set(stack)) > 1:
            try:
                immich.create_stack(list(dict.fromkeys(stack)))
            except ImmichError as exc:
                self.warn(f"{item.filename}: uploaded, but couldn't stack its versions ({exc})")

        self.state.save_item(item.uuid, current, "done", stack[0], ids)
        self.mark(item, DONE, sent)

    def run_items(self) -> None:
        options = self.options
        todo: list[Item] = []
        for item in self.scan.items:
            if item.hidden and options.hidden == "skip":
                self.mark(item, EXCLUDED)
            elif item.missing:
                self.mark(item, MISSING)
            else:
                previous = self.known.get(item.uuid)
                if (
                    previous
                    and previous["status"] == "done"
                    and previous["fingerprint"] == fingerprint(item, options)
                ):
                    self.mark(item, SKIPPED)
                else:
                    todo.append(item)

        self.phase = "uploading"

        def work(item: Item) -> None:
            if self.stop_event.is_set():
                return
            try:
                self.sync_item(item)
                with self.lock:
                    self.failures_in_a_row = 0
            except (ImmichError, OSError) as exc:
                log.warning("Couldn't send %s (%s): %s", item.filename, item.uuid, exc)
                self.state.save_item(
                    item.uuid, fingerprint(item, options), "failed", None, {}, str(exc)
                )
                self.mark(item, FAILED)
                with self.lock:
                    self.failures_in_a_row += 1
                    tripped = self.failures_in_a_row >= STOP_AFTER_FAILURES_IN_A_ROW
                if tripped and not self.stop_event.is_set():
                    self.error = (
                        f"Stopped after {STOP_AFTER_FAILURES_IN_A_ROW} failures in a row. "
                        f"Last error: {exc}"
                    )
                    self.stop_event.set()

        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            # Submitted in small batches so a Stop takes effect quickly on huge libraries.
            batch = options.workers * 4
            for start in range(0, len(todo), batch):
                if self.stop_event.is_set():
                    break
                for future in as_completed(
                    [pool.submit(work, item) for item in todo[start : start + batch]]
                ):
                    future.result()

    def run_albums(self) -> None:
        self.phase = "albums"
        records = self.state.all_items()
        wanted: dict[str, list[str]] = {}
        for item in self.scan.items:
            record = records.get(item.uuid)
            if not record or not record["primary_id"] or record["status"] != "done":
                continue
            if item.hidden and self.options.hidden == "skip":
                continue
            for title in item.albums:
                wanted.setdefault(title, []).append(record["primary_id"])

        self.albums_total = len(wanted)
        existing = self.immich.albums()
        for title, asset_ids in wanted.items():
            if self.stop_event.is_set():
                return
            try:
                album_id = self.state.album_id(title)
                if album_id is None and title in existing:
                    album_id = existing[title]["id"]
                if album_id is None:
                    album_id = self.immich.create_album(title)
                self.state.save_album(title, album_id)
                current = self.immich.album_asset_ids(album_id)
                pending = [a for a in dict.fromkeys(asset_ids) if a not in current]
                for start in range(0, len(pending), ALBUM_CHUNK):
                    self.immich.add_to_album(album_id, pending[start : start + ALBUM_CHUNK])
            except ImmichError as exc:
                self.warn(f"Album {title}: {exc}")
            self.albums_done += 1

    def run(self) -> None:
        log.info("Sync started: %d items, options %s", len(self.scan.items), asdict(self.options))
        try:
            self.run_items()
            if self.options.albums and not self.stop_event.is_set():
                self.run_albums()
            if self.error:
                self.phase = "failed"
            elif self.stop_event.is_set():
                self.phase = "stopped"
            else:
                self.phase = "finished"
        except (ImmichError, OSError) as exc:
            log.exception("Sync failed")
            self.error = str(exc)
            self.phase = "failed"
        status = self.status()
        status["finishedAt"] = datetime.now(UTC).isoformat()
        log.info(
            "Sync %s: %d sent, %d unchanged, %d failed, %d missing",
            status["phase"],
            status["done"],
            status["skipped"],
            status["failed"],
            status["missing"],
        )
        self.state.set_meta("last_sync", status)


def verify(scan: Scan, state: State, immich: Immich, options: Options) -> dict[str, Any]:
    """Ask Immich what it holds, rather than trusting our own records."""
    records = state.all_items()
    expected: dict[str, Item] = {}
    for item in scan.items:
        if item.missing or (item.hidden and options.hidden == "skip"):
            continue
        expected[item.uuid] = item

    present = set(immich.search_ids())
    present |= set(immich.search_ids(visibility="archive"))

    not_synced, gone = [], []
    for uuid, item in expected.items():
        record = records.get(uuid)
        if not record or record["status"] != "done":
            not_synced.append(item.filename)
        elif record["primary_id"] not in present:
            gone.append(item.filename)

    album_rows: list[dict[str, Any]] = []
    if options.albums:
        members: dict[str, set[str]] = {title: set() for title in scan.album_names}
        for uuid, item in expected.items():
            primary = records.get(uuid, {}).get("primary_id")
            if primary:
                for title in item.albums:
                    members[title].add(primary)
        for title in scan.album_names:
            ids = members[title]
            album_id = state.album_id(title)
            actual = immich.album_asset_ids(album_id) if album_id else set()
            album_rows.append(
                {
                    "title": title,
                    "expected": len(ids),
                    "matched": len(ids & actual),
                    "extra": len(actual - ids),
                    "exists": album_id is not None,
                }
            )

    report: dict[str, Any] = {
        "expected": len(expected),
        "confirmed": len(expected) - len(not_synced) - len(gone),
        "notSynced": not_synced[:200],
        "notSyncedCount": len(not_synced),
        "goneFromImmich": gone[:200],
        "goneCount": len(gone),
        "missingFromLibrary": [i.filename for i in scan.items if i.missing][:200],
        "missingCount": sum(1 for i in scan.items if i.missing),
        "albums": album_rows,
        "albumsWithGaps": sum(1 for row in album_rows if row["matched"] < row["expected"]),
    }
    report["clean"] = not (
        report["notSyncedCount"] or report["goneCount"] or report["albumsWithGaps"]
    )
    report["checkedAt"] = datetime.now(UTC).isoformat()
    state.set_meta("last_verify", report)
    return report
