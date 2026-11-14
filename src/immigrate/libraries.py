"""The libraries Immigrate syncs. Each keeps its own records, read, and choices."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .check import Check, run_check
from .folder_library import folder_marker, scan_folder
from .folders import FOLDER, PHOTOS
from .immich import Immich
from .library import Scan, scan_library
from .state import State
from .sync import Options

log = logging.getLogger(__name__)

SCAN_LOG_SECONDS = 30
LIBRARY_SUFFIX = ".photoslibrary"


def library_id(path: str) -> str:
    """Derive the ID from the path, so adding a library again finds its old records."""
    return hashlib.sha1(path.encode(), usedforsecurity=False).hexdigest()[:12]


def folder_for(data_dir: Path, path: str) -> Path:
    return data_dir / "libraries" / library_id(path)


class Library:
    def __init__(
        self,
        data_dir: Path,
        path: str,
        options: dict[str, Any] | None = None,
        auto_minutes: int = 0,
        kind: str = PHOTOS,
    ) -> None:
        self.id = library_id(path)
        self.path = path
        # Saved rather than worked out from the path. Older versions could point at a
        # Photos library whose path doesn't end in .photoslibrary.
        self.kind = kind
        self.options = Options.from_dict(options)
        self.auto_minutes = auto_minutes
        self.folder = folder_for(data_dir, path)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = State(self.folder / "state.db")
        self.scan_path = self.folder / "scan.json.gz"
        self.scan = self.load_scan()
        self.check_path = self.folder / "check.json.gz"
        self.check = self.load_check()
        self.scan_running = False  # true while reading or checking
        self.scan_phase = "reading"  # then checksums, details, and immich while checking
        self.scan_progress = (0, 0)
        self.scan_error: str | None = None
        self.check_error: str | None = None
        self.stop_event = threading.Event()
        self.scan_logged_at: float | None = None
        self.queued = False  # a sync starts when the current read ends
        self.last_check = float(self.state.get_meta("last_check", 0.0))

    @property
    def name(self) -> str:
        return Path(self.path).name.removesuffix(LIBRARY_SUFFIX)

    def to_settings(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "options": asdict(self.options),
            "autoMinutes": self.auto_minutes,
        }

    def close(self) -> None:
        self.state.close()

    def load_scan(self) -> Scan | None:
        """Return the last saved read, so a restart doesn't lose it."""
        if not self.scan_path.exists():
            return None
        try:
            with gzip.open(self.scan_path, "rt") as file:
                scan = Scan.from_dict(json.load(file))
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("Couldn't load the saved read of %s, so it needs reading again", self.name)
            return None
        return scan if scan.library == self.path else None

    def load_check(self) -> Check | None:
        """Return the saved check, if it belongs to the saved read."""
        if self.scan is None or not self.check_path.exists():
            return None
        try:
            with gzip.open(self.check_path, "rt") as file:
                check = Check.from_dict(json.load(file))
        except (OSError, ValueError, KeyError, TypeError):
            log.warning(
                "Couldn't load the saved check of %s, so it needs checking again", self.name
            )
            return None
        same = check.library == self.path and check.scanned_at == self.scan.scanned_at
        return check if same else None

    def save_check(self, check: Check) -> None:
        temporary = self.check_path.with_suffix(".tmp")
        try:
            with gzip.open(temporary, "wt") as file:
                json.dump(check.to_dict(), file)
            temporary.replace(self.check_path)
        except OSError:
            log.warning("Couldn't save the check of %s, so it will be lost on restart", self.name)

    def save_scan(self, scan: Scan) -> None:
        temporary = self.scan_path.with_suffix(".tmp")
        try:
            with gzip.open(temporary, "wt") as file:
                json.dump(scan.to_dict(), file)
            temporary.replace(self.scan_path)
        except OSError:
            log.warning("Couldn't save the read of %s, so it will be lost on restart", self.name)

    def change_marker(self) -> float | str | None:
        """Return a value that differs once the library has changed, or None if it's unreadable."""
        try:
            if self.kind == FOLDER:
                return folder_marker(self.path)
            return os.stat(Path(self.path) / "database" / "Photos.sqlite").st_mtime
        except OSError:
            return None

    def record_check(self, now: float) -> None:
        self.last_check = now
        self.state.set_meta("last_check", now)

    def report_scan(self, done: int, total: int) -> None:
        self.scan_progress = (done, total)
        now = time.monotonic()
        if self.scan_logged_at is None or now - self.scan_logged_at >= SCAN_LOG_SECONDS:
            self.scan_logged_at = now
            if total:
                log.info("Read %d of %d items", done, total)
            else:
                log.info("Found %d items so far", done)

    def report_check(self, phase: str, done: int, total: int) -> None:
        self.scan_phase = phase
        self.scan_progress = (done, total)
        now = time.monotonic()
        if self.scan_logged_at is None or now - self.scan_logged_at >= SCAN_LOG_SECONDS:
            self.scan_logged_at = now
            log.info("Checking %s: %d of %d", phase, done, total)

    def check_now(self, connect: Callable[[], Immich | None]) -> None:
        """Check the current read in this thread. Immich is only asked when connected."""
        if self.scan is None:
            return
        self.scan_phase, self.scan_progress, self.check_error = "checksums", (0, 0), None
        immich = None
        try:
            immich = connect()
            check = run_check(
                self.scan,
                self.state,
                immich,
                self.kind == FOLDER,
                self.stop_event,
                self.report_check,
            )
            self.check = check
            self.save_check(check)
            found = check.summary()
            log.info(
                "Checked %s: %d copies, %d already in Immich, %d without a date, "
                "%d without a location",
                self.name,
                found["copies"],
                found["inImmich"],
                found["missingDate"],
                found["missingLocation"],
            )
        except Exception as exc:
            log.exception("Couldn't check the library at %s", self.path)
            self.check_error = f"Couldn't finish checking the library. Details: {exc}"
        finally:
            if immich is not None:
                immich.close()

    def read(self, connect: Callable[[], Immich | None] | None = None) -> None:
        """Read the library in this thread, then check it when `connect` is given.

        The caller sets `scan_running` first.
        """
        try:
            self.read_only()
            if connect is not None and self.scan_error is None and not self.stop_event.is_set():
                self.check_now(connect)
        finally:
            self.scan_running = False

    def check_only(self, connect: Callable[[], Immich | None]) -> None:
        try:
            self.check_now(connect)
        finally:
            self.scan_running = False

    def read_only(self) -> None:
        self.scan_phase = "reading"
        marker = self.change_marker()
        log.info("Reading the library at %s", self.path)
        scan = scan_folder if self.kind == FOLDER else scan_library
        try:
            self.scan = scan(self.path, self.report_scan)
            log.info("Scanned %s: %d items", self.path, len(self.scan.items))
            if self.scan.unreadable:
                log.warning("Couldn't open %d folders", len(self.scan.unreadable))
            self.save_scan(self.scan)
            self.check = None  # it belonged to the earlier read
            self.state.set_meta("scan_mtime", marker)
        except Exception as exc:
            log.exception("Couldn't read the library at %s", self.path)
            self.scan_error = (
                f"Couldn't read the library at {self.path}. Check that the container "
                f"can read it. Details: {exc}"
            )

    def start(self, target: Callable[[], None]) -> threading.Thread:
        self.scan_running = True
        self.scan_error = self.check_error = None
        self.scan_progress = (0, 0)
        self.scan_logged_at = None
        self.stop_event.clear()
        thread = threading.Thread(target=target, daemon=True, name=f"scan-{self.id}")
        thread.start()
        return thread

    def start_read(self, connect: Callable[[], Immich | None] | None = None) -> threading.Thread:
        return self.start(lambda: self.read(connect))

    def start_check(self, connect: Callable[[], Immich | None]) -> threading.Thread:
        return self.start(lambda: self.check_only(connect))


def migrate_single_library(data_dir: Path, path: str) -> None:
    """Move the records of the one library that older versions kept into its own folder."""
    folder = folder_for(data_dir, path)
    if (folder / "state.db").exists():
        return
    folder.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        old = data_dir / f"photobridge.db{suffix}"
        if old.exists():
            old.replace(folder / f"state.db{suffix}")
    old_scan = data_dir / "scan.json.gz"
    if old_scan.exists():
        old_scan.replace(folder / "scan.json.gz")
