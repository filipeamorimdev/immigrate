"""The libraries Immigrate syncs. Each keeps its own records, read, and choices."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

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
    ) -> None:
        self.id = library_id(path)
        self.path = path
        self.options = Options.from_dict(options)
        self.auto_minutes = auto_minutes
        self.folder = folder_for(data_dir, path)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state = State(self.folder / "state.db")
        self.scan_path = self.folder / "scan.json.gz"
        self.scan = self.load_scan()
        self.scan_running = False
        self.scan_progress = (0, 0)
        self.scan_error: str | None = None
        self.scan_logged_at: float | None = None
        self.queued = False  # a sync starts when the current read ends
        self.last_check = float(self.state.get_meta("last_check", 0.0))

    @property
    def name(self) -> str:
        return Path(self.path).name.removesuffix(LIBRARY_SUFFIX)

    def to_settings(self) -> dict[str, Any]:
        return {
            "path": self.path,
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

    def save_scan(self, scan: Scan) -> None:
        temporary = self.scan_path.with_suffix(".tmp")
        try:
            with gzip.open(temporary, "wt") as file:
                json.dump(scan.to_dict(), file)
            temporary.replace(self.scan_path)
        except OSError:
            log.warning("Couldn't save the read of %s, so it will be lost on restart", self.name)

    def mtime(self) -> float | None:
        try:
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
            log.info("Read %d of %d items", done, total)

    def read(self) -> None:
        """Read the library in this thread. The caller sets `scan_running` first."""
        mtime = self.mtime()
        log.info("Reading the library at %s", self.path)
        try:
            self.scan = scan_library(self.path, self.report_scan)
            log.info("Scanned %s: %d items", self.path, len(self.scan.items))
            self.save_scan(self.scan)
            self.state.set_meta("scan_mtime", mtime)
        except Exception as exc:
            log.exception("Couldn't read the library at %s", self.path)
            self.scan_error = (
                f"Couldn't read the library at {self.path}. Check that the container "
                f"can read it. Details: {exc}"
            )
        finally:
            self.scan_running = False

    def start_read(self) -> threading.Thread:
        self.scan_running = True
        self.scan_error = None
        self.scan_progress = (0, 0)
        self.scan_logged_at = None
        thread = threading.Thread(target=self.read, daemon=True, name=f"scan-{self.id}")
        thread.start()
        return thread


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
