"""One-way sync from an Apple Photos library or a plain folder into Immich.

Nothing here deletes anything: not in the library, not in Immich. A photo
removed from Apple Photos or from the folder stays in Immich.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .check import Check
from .immich import Immich, ImmichError, Upload
from .library import Item, Part, Scan
from .state import State
from .xmp import build_sidecar

log = logging.getLogger(__name__)

ALBUM_CHUNK = 500
STOP_AFTER_FAILURES_IN_A_ROW = 20

FIX_CHUNK = 500

# What the page calls each file of an item while a worker sends it.
PART_NAMES = {"main": "original", "edit": "edit", "raw": "RAW", "live": "Live Photo video"}

# Single-character cell codes for the mosaic on the sync screen.
WAITING, DONE, SKIPPED, FAILED, MISSING, EXCLUDED = "w", "d", "s", "f", "m", "x"


@dataclass
class Options:
    edits: str = "stack"  # "stack" keeps the edit as the cover, "original" skips edits
    albums: bool = True
    hidden: str = "skip"  # "skip" or "archive"
    people_as_tags: bool = True
    copies: str = "include"  # identical files in the library: "include" or "skip"
    existing: str = "link"  # items Immich already has from elsewhere: "link" or "skip"
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
        if options.copies not in ("include", "skip"):
            raise ValueError("copies must be 'include' or 'skip'")
        if options.existing not in ("link", "skip"):
            raise ValueError("existing must be 'link' or 'skip'")
        options.workers = max(1, min(int(options.workers), 8))
        return options


def size_text(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def sidecar_for(item: Item, options: Options) -> bytes | None:
    """Build a Photos item's sidecar. A folder's files are sent with what they already have.

    Immich prefers a sidecar's dates to the file's own, and all a folder knows is when
    the file last changed, so building one would replace real capture dates.
    """
    if not item.from_folder:
        return build_sidecar(item, options.people_as_tags)
    return Path(item.sidecar_path).read_bytes() if item.sidecar_path else None


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


def left_out(
    item: Item, options: Options, check: Check | None, record: dict[str, Any] | None
) -> bool:
    """Whether the choices leave this item out of the sync."""
    if item.hidden and options.hidden == "skip":
        return True
    if check is None:
        return False
    if item.uuid in check.copies:
        return options.copies == "skip"
    # Something Immigrate already linked or sent stays in step with the library.
    already = record is not None and record["status"] == "done"
    return options.existing == "skip" and not already and check.existing_asset(item) is not None


def plan(scan: Scan, state: State, options: Options, check: Check | None = None) -> dict[str, Any]:
    """Work out what a sync would do, without contacting Immich."""
    known = state.all_items()
    new = changed = unchanged = excluded = missing = retry = linked = 0
    new_bytes = 0
    # Files that won't need uploading: Immich has them, or an earlier item sends them.
    held = set(check.in_immich) if check else set()
    for item in scan.items:
        previous = known.get(item.uuid)
        if left_out(item, options, check, previous):
            excluded += 1
        elif item.missing:
            missing += 1
        else:
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
            sent = 0
            for part in item.parts:
                if part.role == "edit" and options.edits != "stack":
                    continue
                checksum = check.checksum(part.path) if check else None
                if checksum and checksum in held:
                    continue
                if checksum:
                    held.add(checksum)
                sent += part.size
            if not sent:
                linked += 1
            new_bytes += sent
    return {
        "new": new,
        "changed": changed,
        "retry": retry,
        "unchanged": unchanged,
        "excluded": excluded,
        "missing": missing,
        "linked": linked,
        "bytes": new_bytes,
        "copies": len(check.copies) if check else 0,
        "existing": sum(
            1
            for item in scan.items
            if check
            and item.uuid not in check.copies
            and check.existing_asset(item)
            and (known.get(item.uuid) or {}).get("status") != "done"
        ),
        "albums": len(scan.album_names) if options.albums else 0,
        "newAlbums": sum(1 for a in scan.album_names if not state.album_id(a))
        if options.albums
        else 0,
    }


class Job:
    def __init__(
        self,
        scan: Scan,
        state: State,
        immich: Immich,
        options: Options,
        check: Check | None = None,
        note: Callable[[str], None] | None = None,
    ):
        self.scan, self.state, self.immich, self.options = scan, state, immich, options
        self.check = check
        # Adds a line to the log on the page. Each sent item gets one.
        self.note = note or (lambda _: None)
        self.started_at = time.time()
        # What each upload thread is sending now, by thread, and the number the page shows.
        self.active: dict[int, dict[str, Any]] = {}
        self.slots: dict[int, int] = {}
        self.todo_total = 0
        self.handled = 0
        self.bytes_total = 0
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
        self.fixes_done = 0
        self.fixes_total = 0
        self.known = state.all_items()
        # Checksums of files Immich already holds, and the asset each one is.
        self.held: dict[str, str] = dict(check.in_immich) if check else {}
        self.held_at_start = frozenset(self.held)

    def mark(self, item: Item, code: str, added_bytes: int = 0) -> None:
        with self.lock:
            self.cells[self.index[item.uuid]] = code
            self.bytes_done += added_bytes
            if added_bytes and (worker := self.active.get(threading.get_ident())):
                # Now counted in `bytes_done`, so it isn't counted twice.
                worker["counted"] = True

    def begin(self, item: Item) -> None:
        """Note that this thread's worker has picked up an item."""
        ident = threading.get_ident()
        with self.lock:
            slot = self.slots.setdefault(ident, len(self.slots) + 1)
            self.active[ident] = {
                "id": slot,
                "file": item.filename,
                "part": None,
                "sent": 0,
                "size": self.upload_size(item),
                "base": 0,
                "counted": False,
            }

    def end(self) -> None:
        with self.lock:
            self.active.pop(threading.get_ident(), None)
            self.handled += 1

    def upload_size(self, item: Item) -> int:
        return sum(p.size for p in item.parts if p.role != "edit" or self.options.edits == "stack")

    def workers(self) -> list[dict[str, Any]]:
        """One row for each worker, in order. Call with the lock held."""
        busy = {worker["id"]: worker for worker in self.active.values()}
        rows = []
        for slot in range(1, self.options.workers + 1):
            worker = busy.get(slot)
            rows.append(
                {
                    "id": slot,
                    "file": worker["file"] if worker else None,
                    "part": worker["part"] if worker else None,
                    "sent": min(worker["sent"], worker["size"]) if worker else 0,
                    "size": worker["size"] if worker else 0,
                }
            )
        return rows

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
                # Includes what the workers have sent of the files they're on now.
                "bytesSent": self.bytes_done
                + sum(w["sent"] for w in self.active.values() if not w["counted"]),
                "bytesTotal": self.bytes_total,
                "todo": self.todo_total,
                "handled": self.handled,
                "startedAt": self.started_at,
                "workers": self.workers() if self.phase == "uploading" else [],
                "albumsDone": self.albums_done,
                "albumsTotal": self.albums_total,
                "fixesDone": self.fixes_done,
                "fixesTotal": self.fixes_total,
                "warnings": list(self.warnings[-5:]),
                "warningCount": len(self.warnings),
                "options": asdict(self.options),
            }

    def mosaic(self) -> str:
        with self.lock:
            return "".join(self.cells)

    def send(self, part: Part, created: str, modified: str, **fields: Any) -> Upload:
        """Upload a file, unless Immich is known to hold it already."""
        path = part.path
        known = self.check.checksum(path) if self.check else None
        checksum = known if known and self.check and self.check.still_matches(path) else None
        with self.lock:
            worker = self.active.get(threading.get_ident())
            if worker:
                worker["part"] = PART_NAMES.get(part.role)
                worker["base"] = worker["sent"]
        with self.lock:
            held = self.held.get(checksum) if checksum else None
            # The total assumed that files Immich had at the start are linked, and the
            # rest uploaded. This file turned out the other way, so the total follows.
            expected_link = known in self.held_at_start
            if held and not expected_link:
                self.bytes_total = max(self.bytes_total - part.size, 0)
            elif not held and expected_link:
                self.bytes_total += part.size
            if held and worker:
                # Nothing to upload for this file, so the worker's bar doesn't wait for it.
                worker["size"] = max(worker["size"] - part.size, 0)
        if held:
            return Upload(held, duplicate=True, sent=False)

        def progress(position: int) -> None:
            # A retry starts the file again, so this is a position, not an amount to add.
            if worker:
                worker["sent"] = worker["base"] + position

        result = self.immich.upload(
            path, part.filename, created, modified, progress=progress, **fields
        )
        if checksum:
            with self.lock:
                self.held.setdefault(checksum, result.id)
        return result

    def sync_item(self, item: Item) -> None:
        options = self.options
        current = fingerprint(item, options)
        created, modified = item.taken.isoformat(), item.modified.isoformat()
        visibility = "archive" if item.hidden else "timeline"
        sidecar = sidecar_for(item, options)
        previous = self.known.get(item.uuid) or {}
        ids: dict[str, str] = dict(previous.get("asset_ids") or {})
        sent = 0

        live_id = None
        if live := item.part("live"):
            upload = self.send(live, created, modified, visibility="hidden")
            live_id = ids["live"] = upload.id
            sent += live.size if upload.sent else 0

        main = item.part("main")
        if main is None:
            raise OSError(f"{item.filename} has no original on disk")
        result = self.send(
            main,
            created,
            modified,
            favorite=item.favorite,
            visibility=visibility,
            live_video_id=live_id,
            sidecar=sidecar,
        )
        ids["main"] = result.id
        sent += main.size if result.sent else 0
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
                    self.immich.update_asset(result.id, **changes)
                except ImmichError as exc:
                    self.warn(f"{item.filename}: couldn't update the existing copy ({exc})")

        stack: list[str] = []
        if (edit := item.part("edit")) and options.edits == "stack":
            upload = self.send(
                edit,
                created,
                modified,
                favorite=item.favorite,
                visibility=visibility,
                sidecar=sidecar,
            )
            ids["edit"] = upload.id
            sent += edit.size if upload.sent else 0
            stack.append(ids["edit"])
        stack.append(ids["main"])
        if raw := item.part("raw"):
            upload = self.send(
                raw,
                created,
                modified,
                visibility=visibility,
                sidecar=sidecar,
            )
            ids["raw"] = upload.id
            sent += raw.size if upload.sent else 0
            stack.append(ids["raw"])

        if len(set(stack)) > 1:
            try:
                self.immich.create_stack(list(dict.fromkeys(stack)))
            except ImmichError as exc:
                self.warn(f"{item.filename}: uploaded, but couldn't stack its versions ({exc})")

        self.state.save_item(item.uuid, current, "done", stack[0], ids)
        # Linked to what Immich already had, without sending anything.
        self.mark(item, DONE if sent or not self.check else SKIPPED, sent)
        if sent:
            self.note(f"Sent {item.filename} ({size_text(sent)})")
        else:
            self.note(f"Linked {item.filename}, which Immich already has")

    def run_items(self) -> None:
        options = self.options
        todo: list[Item] = []
        for item in self.scan.items:
            previous = self.known.get(item.uuid)
            if left_out(item, options, self.check, previous):
                self.mark(item, EXCLUDED)
            elif item.missing:
                self.mark(item, MISSING)
            elif (
                previous
                and previous["status"] == "done"
                and previous["fingerprint"] == fingerprint(item, options)
            ):
                self.mark(item, SKIPPED)
            else:
                todo.append(item)
        # Copies go last, so they find the asset their original made instead of uploading.
        copies = self.check.copies if self.check else {}
        todo.sort(key=lambda item: item.uuid in copies)

        # What's left to upload, leaving out files Immich already has. A file that another
        # item sends first is taken off the total when it's linked instead.
        for item in todo:
            for part in item.parts:
                if part.role == "edit" and options.edits != "stack":
                    continue
                checksum = self.check.checksum(part.path) if self.check else None
                if checksum not in self.held_at_start:
                    self.bytes_total += part.size
        self.todo_total = len(todo)
        self.phase = "uploading"
        log.info(
            "Sending %d items (%s) with %d workers",
            len(todo),
            size_text(self.bytes_total),
            options.workers,
        )

        def work(item: Item) -> None:
            if self.stop_event.is_set():
                return
            self.begin(item)
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
            finally:
                self.end()

        first = [item for item in todo if item.uuid not in copies]
        later = [item for item in todo if item.uuid in copies]
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            for group in (first, later):
                # Submitted in small batches so a Stop takes effect quickly on huge libraries.
                batch = options.workers * 4
                for start in range(0, len(group), batch):
                    if self.stop_event.is_set():
                        break
                    for future in as_completed(
                        [pool.submit(work, item) for item in group[start : start + batch]]
                    ):
                        future.result()

    def run_fixes(self) -> None:
        """Set the dates and locations the user chose, on items already in Immich."""
        self.phase = "fixing"
        fixes = self.state.fixes()
        if not fixes:
            return
        records = self.state.all_items()
        groups: dict[tuple[Any, ...], list[tuple[str, list[str], str]]] = {}
        for item in self.scan.items:
            fix, record = fixes.get(item.uuid), records.get(item.uuid)
            if not fix or not record or record["status"] != "done":
                continue
            if left_out(item, self.options, self.check, record):
                continue
            asset_ids = sorted(set(record["asset_ids"].values()))
            values = (fix["date"], fix["latitude"], fix["longitude"])
            applied = repr((asset_ids, values))
            if fix["applied"] == applied:
                continue
            groups.setdefault(values, []).append((item.uuid, asset_ids, applied))
        self.fixes_total = sum(len(group) for group in groups.values())
        if self.fixes_total:
            log.info("Setting dates and locations on %d items", self.fixes_total)
        for (date, latitude, longitude), entries in groups.items():
            if self.stop_event.is_set():
                return
            fields: dict[str, Any] = {}
            if date:
                fields["dateTimeOriginal"] = date
            if latitude is not None and longitude is not None:
                fields.update(latitude=latitude, longitude=longitude)
            if not fields:
                continue
            asset_ids = list(dict.fromkeys(a for _, ids, _ in entries for a in ids))
            try:
                self.immich.update_assets(asset_ids, **fields)
            except ImmichError as exc:
                self.warn(f"Couldn't set the date or location of {len(entries)} items ({exc})")
                continue
            for uuid, _, applied in entries:
                self.state.mark_fix_applied(uuid, applied)
            self.fixes_done += len(entries)

    def run_albums(self) -> None:
        self.phase = "albums"
        records = self.state.all_items()
        wanted: dict[str, list[str]] = {}
        for item in self.scan.items:
            record = records.get(item.uuid)
            if not record or not record["primary_id"] or record["status"] != "done":
                continue
            if left_out(item, self.options, self.check, record):
                continue
            for title in item.albums:
                wanted.setdefault(title, []).append(record["primary_id"])

        self.albums_total = len(wanted)
        log.info("Building %d albums", len(wanted))
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
            self.note(f"Album {title} is up to date ({len(asset_ids)} items)")

    def run(self) -> None:
        log.info("Sync started: %d items in the library", len(self.scan.items))
        log.debug("Sync options: %s", asdict(self.options))
        try:
            self.run_items()
            if not self.stop_event.is_set():
                self.run_fixes()
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


def verify(
    scan: Scan, state: State, immich: Immich, options: Options, check: Check | None = None
) -> dict[str, Any]:
    """Ask Immich what it holds, rather than trusting our own records."""
    records = state.all_items()
    expected: dict[str, Item] = {}
    for item in scan.items:
        if item.missing or left_out(item, options, check, records.get(item.uuid)):
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
