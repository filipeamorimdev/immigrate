"""FastAPI application and the in-process runtime behind it."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import __version__, timezones
from .config import Config
from .folders import FolderError, display_path, library_at, list_folders
from .immich import Immich, ImmichError
from .libraries import Library, library_id, migrate_single_library
from .sync import Job, Options, plan, verify

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
AUTO_SYNC_POLL_SECONDS = 30
ALLOWED_AUTO_MINUTES = (0, 15, 60, 360, 1440)
DEFAULT_AUTO_MINUTES = 60
# osxphotos names the folder it copies the Photos database into like this.
TEMP_PREFIX = "osxphotos_"


def parse_minutes(value: Any) -> int:
    try:
        minutes = int(value or 0)
    except (TypeError, ValueError):
        minutes = -1
    if minutes not in ALLOWED_AUTO_MINUTES:
        raise HTTPException(422, "Choose off, 15 minutes, 1 hour, 6 hours, or 1 day.")
    return minutes


def parse_options(value: Any) -> Options:
    try:
        return Options.from_dict(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


class Runtime:
    """Holds the libraries, and the one read and one sync this single-user service runs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        # osxphotos copies the Photos database here, so it needs real disk space, not RAM.
        self.temp_dir = Path(os.environ.get("TMPDIR", config.data_dir / "tmp"))
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = config.data_dir / "settings.json"
        self.lock = threading.Lock()
        self.job: Job | None = None
        self.job_library: str | None = None
        self.job_thread: threading.Thread | None = None
        self.settings = self.load_settings()
        self.settings["timeZone"] = timezones.initial(self.settings.get("timeZone"))
        timezones.use(self.settings["timeZone"])
        self.libraries: dict[str, Library] = {}
        for entry in self.settings["libraries"]:
            minutes = int(entry.get("autoMinutes") or 0)
            library = Library(config.data_dir, entry["path"], entry.get("options"), minutes)
            self.libraries[library.id] = library

    def close(self) -> None:
        for library in self.libraries.values():
            library.close()

    def load_settings(self) -> dict[str, Any]:
        settings: dict[str, Any] = {}
        if self.settings_path.exists():
            settings = json.loads(self.settings_path.read_text())
        upgrade = "libraries" not in settings
        if upgrade:
            settings = self.upgrade_settings(settings)
        if self.config.immich_url:
            settings["url"] = self.config.immich_url
        if self.config.immich_api_key:
            settings["key"] = self.config.immich_api_key
        if upgrade:
            self.write_settings(settings)
        return settings

    def upgrade_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Turn the single-library settings of older versions into a list of libraries."""
        options = asdict(Options.from_dict(settings.pop("options", None)))
        had_schedule = "autoMinutes" in settings
        minutes = int(settings.pop("autoMinutes", 0) or 0)
        path = settings.pop("library", None) or self.config.library_path
        settings["defaults"] = {
            "options": options,
            "autoMinutes": minutes if had_schedule else DEFAULT_AUTO_MINUTES,
        }
        settings["libraries"] = []
        if path:
            migrate_single_library(self.config.data_dir, path)
            settings["libraries"].append({"path": path, "options": options, "autoMinutes": minutes})
        return settings

    def write_settings(self, settings: dict[str, Any]) -> None:
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.touch(mode=0o600, exist_ok=True)
        temporary.write_text(json.dumps(settings))
        temporary.replace(self.settings_path)

    def save_settings(self) -> None:
        self.settings["libraries"] = [lib.to_settings() for lib in self.libraries.values()]
        self.write_settings(self.settings)

    def require_connection(self) -> None:
        if not self.settings.get("url") or not self.settings.get("key"):
            raise HTTPException(409, "Connect to Immich first.")

    def immich(self) -> Immich:
        self.require_connection()
        return Immich(self.settings["url"], self.settings["key"])

    def library(self, library_id: str) -> Library:
        library = self.libraries.get(library_id)
        if library is None:
            raise HTTPException(404, "Immigrate isn't syncing that library.")
        return library

    def add_library(self, path: str) -> Library:
        with self.lock:
            library = self.libraries.get(library_id(path))
            if library is None:
                # Not on a schedule until its first sync, so nothing runs before the
                # choices have been seen.
                library = Library(self.config.data_dir, path, self.settings["defaults"]["options"])
                self.libraries[library.id] = library
        self.save_settings()
        return library

    def remove_library(self, library: Library) -> None:
        """Stop syncing a library. Its records stay, so adding it again picks up where it was."""
        with self.lock:
            if self.busy(library):
                raise HTTPException(409, f"Wait for {library.name} to finish reading or syncing.")
            del self.libraries[library.id]
        library.close()
        self.save_settings()

    def job_running(self) -> bool:
        return bool(self.job_thread and self.job_thread.is_alive())

    def syncing(self) -> Library | None:
        if self.job_running() and self.job_library:
            return self.libraries.get(self.job_library)
        return None

    def reading(self) -> Library | None:
        return next((lib for lib in self.libraries.values() if lib.scan_running), None)

    def leftovers(self) -> list[Path]:
        """Database copies left by reads that didn't finish, such as when the container stopped.

        A read in progress is using its copy, so nothing counts as left over until it ends.
        """
        if self.reading():
            return []
        return sorted(self.temp_dir.glob(f"{TEMP_PREFIX}*"))

    def leftover_state(self) -> dict[str, int] | None:
        """What's left over, or None when nothing there takes up space."""
        found = self.leftovers()
        size = sum(
            file.stat().st_size
            for folder in found
            for file in [folder, *folder.rglob("*")]
            if file.is_file()
        )
        return {"count": len(found), "bytes": size} if size else None

    def delete_leftovers(self) -> dict[str, int]:
        with self.lock:
            if other := self.reading():
                raise HTTPException(409, f"Wait for {other.name} to finish reading.")
            freed = self.leftover_state() or {"count": 0, "bytes": 0}
            for path in self.leftovers():
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        log.info("Deleted %d temporary database copies", freed["count"])
        return freed

    def busy(self, library: Library) -> bool:
        return library.scan_running or library.queued or self.syncing() is library

    def start_read(self, library: Library) -> threading.Thread | None:
        with self.lock:
            if library.scan_running:
                return None
            if other := self.reading():
                raise HTTPException(409, f"Wait for {other.name} to finish reading.")
            if self.syncing() is library:
                raise HTTPException(409, "Wait for the sync to finish.")
            return library.start_read()

    def start_job(self, library: Library) -> None:
        if library.scan is None:
            raise HTTPException(409, "Read the library first.")
        with self.lock:
            if self.job_running():
                other = self.syncing()
                name = other.name if other else "the other library"
                raise HTTPException(409, f"Wait for {name} to finish syncing.")
            if library.scan_running:
                raise HTTPException(409, "Wait for the library read to finish.")
            client = self.immich()
            job = Job(library.scan, library.state, client, library.options)
            scan_mtime = library.state.get_meta("scan_mtime")

            def run() -> None:
                try:
                    job.run()
                    if job.phase == "finished" and not job.status()["failed"]:
                        # The schedule skips the library until it changes again.
                        library.state.set_meta("synced_mtime", scan_mtime)
                finally:
                    client.close()

            self.job, self.job_library = job, library.id
            self.job_thread = threading.Thread(target=run, daemon=True, name="sync")
            self.job_thread.start()

    def read_and_sync(self, library: Library) -> None:
        """Read the library again, then sync it with its saved choices. Blocks during the read."""
        library.queued = True
        try:
            thread = self.start_read(library)
            if thread:
                thread.join()
            while library.scan_running:
                time.sleep(1)
            if library.scan_error is None:
                self.start_job(library)
        finally:
            library.queued = False

    def auto_sync_once(self, now: float) -> None:
        """Run the scheduled check of the first library that's due."""
        if self.job_running() or self.reading() or not self.settings.get("key"):
            return
        for library in list(self.libraries.values()):
            minutes = library.auto_minutes
            if not minutes or now - library.last_check < minutes * 60:
                continue
            library.record_check(now)
            mtime = library.mtime()
            if mtime is None or mtime == library.state.get_meta("synced_mtime"):
                continue
            log.info("%s changed, so it's being synced", library.name)
            try:
                self.read_and_sync(library)
            except HTTPException as exc:
                log.warning("Scheduled sync of %s skipped: %s", library.name, exc.detail)
            return

    def auto_sync_loop(self) -> None:
        while True:
            time.sleep(AUTO_SYNC_POLL_SECONDS)
            try:
                self.auto_sync_once(time.time())
            except Exception:
                log.exception("Scheduled check failed")

    def library_state(self, library: Library) -> dict[str, Any]:
        job = self.job if self.job and self.job_library == library.id else None
        minutes = library.auto_minutes
        return {
            "id": library.id,
            "name": library.name,
            "path": display_path(self.config.library_root, library.path),
            "options": asdict(library.options),
            "autoMinutes": minutes,
            "scan": {
                "running": library.scan_running,
                "done": library.scan_progress[0],
                "total": library.scan_progress[1],
                "error": library.scan_error,
                "summary": library.scan.summary() if library.scan else None,
            },
            "queued": library.queued,
            "job": job.status() if job else None,
            "counts": library.state.counts(),
            "lastSync": library.state.get_meta("last_sync"),
            "lastVerify": library.state.get_meta("last_verify"),
            "lastCheck": library.last_check or None,
            "nextCheck": library.last_check + minutes * 60 if minutes else None,
        }


def create_app(config: Config | None = None, *, background: bool = True) -> FastAPI:
    runtime = Runtime(config or Config.from_env())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if background:
            threading.Thread(target=runtime.auto_sync_loop, daemon=True, name="auto-sync").start()
        yield
        runtime.close()

    app = FastAPI(
        title="Immigrate",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(GZipMiddleware, minimum_size=2048)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Stop other websites from driving this LAN service through a visitor's browser."""
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if request.method != "GET" and origin and urlparse(origin).netloc != host:
            return JSONResponse({"detail": "Cross-origin request refused."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_page(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Make browsers ask for the page again after an update instead of reusing an old copy."""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(ImmichError)
    async def immich_error(_: Request, exc: ImmichError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/state")
    def get_state() -> dict[str, Any]:
        defaults = runtime.settings["defaults"]
        syncing = runtime.syncing()
        return {
            "version": __version__,
            "immichUrl": runtime.settings.get("url"),
            "hasKey": bool(runtime.settings.get("key")),
            "keyFromEnv": bool(runtime.config.immich_api_key),
            "timeZone": runtime.settings["timeZone"],
            "defaults": {
                "options": asdict(Options.from_dict(defaults["options"])),
                "autoMinutes": int(defaults["autoMinutes"]),
            },
            "libraries": [runtime.library_state(lib) for lib in runtime.libraries.values()],
            "syncing": syncing.id if syncing else None,
            "leftovers": runtime.leftover_state(),
        }

    @app.post("/api/connect")
    async def connect(request: Request) -> dict[str, Any]:
        body = await request.json()
        url = (body.get("url") or runtime.settings.get("url") or "").strip()
        key = (body.get("key") or runtime.settings.get("key") or "").strip()
        if not url or not key:
            raise HTTPException(422, "Enter the Immich address and an API key.")
        if urlparse(url).scheme not in ("http", "https"):
            raise HTTPException(422, "The Immich address must start with http:// or https://.")
        client = Immich(url, key, retries=1)
        try:
            identity = client.identity()
        except ImmichError as exc:
            if exc.status == 401:
                raise HTTPException(
                    401, "Immich refused that API key. Create a new one in Account Settings."
                ) from exc
            raise HTTPException(502, f"Couldn't reach Immich at {url}. {exc}") from exc
        finally:
            client.close()
        runtime.settings.update(url=url, key=key)
        runtime.save_settings()
        return identity

    @app.post("/api/defaults")
    async def save_defaults(request: Request) -> dict[str, Any]:
        body = await request.json()
        options = parse_options(body.get("options"))
        minutes = parse_minutes(body.get("autoMinutes"))
        defaults = {"options": asdict(options), "autoMinutes": minutes}
        runtime.settings["defaults"] = defaults
        runtime.save_settings()
        return defaults

    @app.get("/api/timezones")
    def time_zones() -> list[str]:
        return list(timezones.names())

    @app.post("/api/timezone")
    async def save_time_zone(request: Request) -> dict[str, str]:
        name = (await request.json()).get("timeZone")
        if not timezones.is_valid(name):
            raise HTTPException(422, "Choose a time zone from the list.")
        runtime.settings["timeZone"] = name
        runtime.save_settings()
        timezones.use(name)
        log.info("Time zone is now %s", name)
        return {"timeZone": name}

    @app.get("/api/folders")
    def folders(path: str = "") -> dict[str, Any]:
        try:
            return list_folders(runtime.config.library_root, path)
        except FolderError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/libraries")
    async def add_library(request: Request) -> dict[str, Any]:
        path = (await request.json()).get("path") or ""
        try:
            found = library_at(runtime.config.library_root, path)
        except FolderError as exc:
            raise HTTPException(422, str(exc)) from exc
        return runtime.library_state(runtime.add_library(str(found)))

    @app.post("/api/libraries/{library_id}/settings")
    async def library_settings(library_id: str, request: Request) -> dict[str, Any]:
        library = runtime.library(library_id)
        body = await request.json()
        options = parse_options(body.get("options"))
        minutes = parse_minutes(body.get("autoMinutes"))
        library.options, library.auto_minutes = options, minutes
        runtime.save_settings()
        return runtime.library_state(library)

    @app.post("/api/libraries/{library_id}/remove")
    def remove_library(library_id: str) -> dict[str, bool]:
        runtime.remove_library(runtime.library(library_id))
        return {"removed": True}

    @app.post("/api/libraries/{library_id}/read")
    def read_library(library_id: str) -> dict[str, bool]:
        runtime.start_read(runtime.library(library_id))
        return {"started": True}

    @app.post("/api/libraries/{library_id}/preview")
    async def preview(library_id: str, request: Request) -> dict[str, Any]:
        library = runtime.library(library_id)
        if library.scan is None:
            raise HTTPException(409, "Read the library first.")
        return plan(library.scan, library.state, parse_options(await request.json()))

    @app.post("/api/libraries/{library_id}/sync")
    async def sync(library_id: str, request: Request) -> dict[str, bool]:
        library = runtime.library(library_id)
        if not (await request.json()).get("read"):
            runtime.start_job(library)
            return {"started": True}
        # Checked here so the page hears about it, rather than only the log.
        runtime.require_connection()
        if other := runtime.syncing():
            raise HTTPException(409, f"Wait for {other.name} to finish syncing.")
        if runtime.busy(library):
            raise HTTPException(409, "Wait for the library read to finish.")

        def run() -> None:
            try:
                runtime.read_and_sync(library)
            except HTTPException as exc:
                log.warning("Sync of %s didn't start: %s", library.name, exc.detail)

        threading.Thread(target=run, daemon=True, name=f"read-and-sync-{library.id}").start()
        return {"started": True}

    @app.post("/api/stop")
    def stop() -> dict[str, bool]:
        if runtime.job:
            runtime.job.stop_event.set()
        return {"stopping": True}

    @app.post("/api/leftovers/delete")
    def delete_leftovers() -> dict[str, int]:
        return runtime.delete_leftovers()

    @app.get("/api/mosaic", response_class=PlainTextResponse)
    def mosaic() -> str:
        return runtime.job.mosaic() if runtime.job else ""

    @app.post("/api/libraries/{library_id}/verify")
    def run_verify(library_id: str) -> dict[str, Any]:
        library = runtime.library(library_id)
        if library.scan is None:
            raise HTTPException(409, "Read the library first.")
        if runtime.syncing() is library:
            raise HTTPException(409, "Wait for the sync to finish.")
        client = runtime.immich()
        try:
            return verify(library.scan, library.state, client, library.options)
        finally:
            client.close()

    @app.get("/api/libraries/{library_id}/failures")
    def failures(library_id: str) -> list[dict[str, Any]]:
        library = runtime.library(library_id)
        rows = [
            {"uuid": uuid, "error": record["error"]}
            for uuid, record in library.state.all_items().items()
            if record["status"] == "failed"
        ]
        names = {i.uuid: i.filename for i in library.scan.items} if library.scan else {}
        return [{**row, "filename": names.get(row["uuid"], row["uuid"])} for row in rows[:200]]

    @app.get("/api/libraries/{library_id}/missing")
    def missing(library_id: str) -> list[dict[str, Any]]:
        library = runtime.library(library_id)
        return library.scan.missing_items() if library.scan else []

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
