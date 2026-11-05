from __future__ import annotations

import gzip
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from immigrate import timezones
from immigrate.config import Config
from immigrate.libraries import SCAN_LOG_SECONDS, Library, folder_for
from immigrate.server import Runtime, create_app
from immigrate.state import State

from .mock_immich import Store, serve


@pytest.fixture
def immich():
    store = Store()
    server = serve(store)
    yield store, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def client(tmp_path, main_library, monkeypatch):
    # Keep osxphotos's database copies, and any leftovers, inside the test folder.
    monkeypatch.setenv("TMPDIR", str(tmp_path / "data" / "tmp"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "data" / "tmp"))
    config = Config(
        data_dir=tmp_path / "data",
        library_path=main_library,
        host="127.0.0.1",
        port=0,
        log_level="INFO",
        immich_url=None,
        immich_api_key=None,
        library_root=str(Path(main_library).parent),
    )
    app = create_app(config, background=False)
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


def wait_for(client, condition, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/state").json()
        if condition(state):
            return state
        time.sleep(0.05)
    raise AssertionError("Timed out")


def first(state):
    return state["libraries"][0]


def connect(client, immich):
    store, url = immich
    assert client.post("/api/connect", json={"url": url, "key": store.key}).status_code == 200


def connect_and_read(client, immich):
    connect(client, immich)
    library_id = first(client.get("/api/state").json())["id"]
    assert client.post(f"/api/libraries/{library_id}/read", json={}).status_code == 200
    state = wait_for(client, lambda s: first(s)["scan"]["summary"])
    return library_id, first(state)


def sync_and_wait(client, library_id, read=False):
    response = client.post(f"/api/libraries/{library_id}/sync", json={"read": read})
    assert response.json() == {"started": True}
    return wait_for(
        client, lambda s: first(s)["job"] and first(s)["job"]["phase"] == "finished", timeout=60
    )


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["version"]


def test_index_and_static_files_are_served(client):
    assert "Immigrate" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    font = "/static/fonts/bricolage-grotesque-latin-wght-normal.woff2"
    assert client.get(font).status_code == 200


def test_page_and_static_files_are_checked_for_updates(client):
    assert client.get("/").headers["cache-control"] == "no-cache"
    style = client.get("/static/style.css")
    assert style.headers["cache-control"] == "no-cache"
    again = client.get("/static/style.css", headers={"If-None-Match": style.headers["etag"]})
    assert again.status_code == 304
    assert "cache-control" not in client.get("/healthz").headers


def test_cross_origin_writes_are_refused(client):
    response = client.post("/api/stop", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_same_origin_writes_are_allowed(client):
    response = client.post("/api/stop", headers={"Origin": "http://testserver"})
    assert response.status_code == 200


def test_bad_key_gets_a_clear_message(client, immich):
    _, url = immich
    response = client.post("/api/connect", json={"url": url, "key": "wrong"})
    assert response.status_code == 401
    assert "refused that API key" in response.json()["detail"]


def test_url_must_be_http(client):
    response = client.post("/api/connect", json={"url": "ftp://nas", "key": "x"})
    assert response.status_code == 422


def test_key_is_saved_privately_and_never_returned(client, immich, tmp_path):
    store, _ = immich
    connect(client, immich)
    assert store.key not in client.get("/api/state").text
    settings = tmp_path / "data" / "settings.json"
    assert settings.stat().st_mode & 0o777 == 0o600


def test_library_path_from_the_environment_is_the_first_library(client, main_library):
    library = first(client.get("/api/state").json())
    assert library["name"] == "Test-26.1"
    assert library["path"] == Path(main_library).name
    assert library["autoMinutes"] == 0


def test_sync_needs_a_read(client, immich):
    connect(client, immich)
    library_id = first(client.get("/api/state").json())["id"]
    assert client.post(f"/api/libraries/{library_id}/sync", json={}).status_code == 409


def test_unknown_library_is_not_found(client):
    assert client.post("/api/libraries/nope/read", json={}).status_code == 404


def test_bad_options_are_rejected(client, immich):
    library_id, _ = connect_and_read(client, immich)
    response = client.post(f"/api/libraries/{library_id}/preview", json={"edits": "nope"})
    assert response.status_code == 422


def test_missing_items_are_listed_after_a_read(client, immich):
    library_id = first(client.get("/api/state").json())["id"]
    assert client.get(f"/api/libraries/{library_id}/missing").json() == []
    connect_and_read(client, immich)
    missing = client.get(f"/api/libraries/{library_id}/missing").json()
    assert sorted(m["filename"] for m in missing) == ["IMG_2000.JPG", "Pumpkins4.jpg"]


def test_full_flow_over_http(client, immich):
    store, _ = immich
    library_id, library = connect_and_read(client, immich)
    assert library["scan"]["summary"]["total"] == 14
    preview = client.post(f"/api/libraries/{library_id}/preview", json={}).json()
    assert preview["new"] == 12
    library = first(sync_and_wait(client, library_id))
    assert library["job"]["done"] == 12
    assert library["counts"] == {"done": 12}
    assert library["lastSync"]["finishedAt"]
    assert len(client.get("/api/mosaic").text) == 14
    report = client.post(f"/api/libraries/{library_id}/verify").json()
    assert report["clean"] and report["checkedAt"] and not store.violations
    preview = client.post(f"/api/libraries/{library_id}/preview", json={}).json()
    assert preview["unchanged"] == 12


def test_start_sync_reads_the_library_first(client, immich):
    connect(client, immich)
    library_id = first(client.get("/api/state").json())["id"]
    library = first(sync_and_wait(client, library_id, read=True))
    assert library["scan"]["summary"]["total"] == 14
    assert library["job"]["done"] == 12


def test_library_settings_are_saved_and_checked(client):
    library_id = first(client.get("/api/state").json())["id"]
    url = f"/api/libraries/{library_id}/settings"
    assert client.post(url, json={"autoMinutes": 7}).status_code == 422
    assert client.post(url, json={"options": {"hidden": "nope"}}).status_code == 422
    body = {"options": {"edits": "original", "albums": False}, "autoMinutes": 60}
    library = client.post(url, json=body).json()
    assert library["autoMinutes"] == 60
    assert library["options"]["edits"] == "original" and not library["options"]["albums"]
    restarted = Runtime(client.app.state.runtime.config)
    try:
        saved = restarted.libraries[library_id]
        assert saved.auto_minutes == 60 and saved.options.edits == "original"
    finally:
        restarted.close()


def test_defaults_are_used_for_new_libraries(client):
    body = {"options": {"hidden": "archive", "people_as_tags": False}, "autoMinutes": 1440}
    assert client.post("/api/defaults", json=body).status_code == 200
    assert client.post("/api/defaults", json={"autoMinutes": 5}).status_code == 422
    state = client.get("/api/state").json()
    assert state["defaults"]["autoMinutes"] == 1440
    library = client.post("/api/libraries", json={"path": "Test-Live-15.7.2.photoslibrary"}).json()
    assert library["options"]["hidden"] == "archive"
    assert not library["options"]["people_as_tags"]
    # Nothing runs on a schedule before the first sync has been started by hand.
    assert library["autoMinutes"] == 0


def test_each_library_keeps_its_own_records(client, immich):
    library_id, _ = connect_and_read(client, immich)
    sync_and_wait(client, library_id)
    added = client.post("/api/libraries", json={"path": "Test-Live-15.7.2.photoslibrary"}).json()
    assert added["id"] != library_id and added["counts"] == {}
    state = client.get("/api/state").json()
    assert [lib["id"] for lib in state["libraries"]] == [library_id, added["id"]]


def test_removing_a_library_keeps_its_records(client, immich):
    library_id, library = connect_and_read(client, immich)
    sync_and_wait(client, library_id)
    assert client.post(f"/api/libraries/{library_id}/remove", json={}).json() == {"removed": True}
    assert client.get("/api/state").json()["libraries"] == []
    path = Path(library["path"]).name
    again = client.post("/api/libraries", json={"path": path}).json()
    assert again["id"] == library_id
    assert again["counts"] == {"done": 12} and again["scan"]["summary"]


def test_adding_a_library_twice_keeps_one(client):
    path = "Test-Live-15.7.2.photoslibrary"
    one = client.post("/api/libraries", json={"path": path}).json()
    two = client.post("/api/libraries", json={"path": path}).json()
    assert one["id"] == two["id"]
    assert len(client.get("/api/state").json()["libraries"]) == 2


def test_scheduled_sync_only_runs_when_the_library_changes(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.update(url="http://x", key="k")
    library = next(iter(runtime.libraries.values()))
    library.auto_minutes = 15
    synced = []
    monkeypatch.setattr(runtime, "read_and_sync", synced.append)
    monkeypatch.setattr(library, "mtime", lambda: 42.0)
    runtime.auto_sync_once(now=10_000.0)
    assert synced == [library] and library.last_check == 10_000.0
    library.state.set_meta("synced_mtime", 42.0)
    runtime.auto_sync_once(now=20_000.0)
    assert len(synced) == 1 and library.last_check == 20_000.0
    runtime.auto_sync_once(now=20_100.0)
    assert library.last_check == 20_000.0, "not due again yet"
    monkeypatch.setattr(library, "mtime", lambda: 43.0)
    runtime.auto_sync_once(now=30_000.0)
    assert len(synced) == 2


def test_libraries_off_the_schedule_are_not_checked(client, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.update(url="http://x", key="k")
    synced = []
    monkeypatch.setattr(runtime, "read_and_sync", synced.append)
    runtime.auto_sync_once(now=10_000.0)
    assert synced == []


def test_scan_progress_is_logged_at_most_every_interval(tmp_path, caplog):
    library = Library(tmp_path / "data", "/library/Test.photoslibrary")
    caplog.set_level(logging.INFO, logger="immigrate.libraries")
    try:
        library.report_scan(50, 1000)
        library.report_scan(100, 1000)
        assert library.scan_logged_at is not None
        library.scan_logged_at -= SCAN_LOG_SECONDS
        library.report_scan(150, 1000)
    finally:
        library.close()
    assert [r.getMessage() for r in caplog.records] == [
        "Read 50 of 1000 items",
        "Read 150 of 1000 items",
    ]


def test_scan_is_restored_after_a_restart(client, immich):
    library_id, library = connect_and_read(client, immich)
    runtime = client.app.state.runtime
    restarted = Runtime(runtime.config)
    try:
        scan = restarted.libraries[library_id].scan
        assert scan is not None
        assert scan.summary() == library["scan"]["summary"]
        assert scan.to_dict() == runtime.libraries[library_id].scan.to_dict()
    finally:
        restarted.close()


def test_saved_scan_of_another_library_is_ignored(client, immich, tmp_path):
    library_id, _ = connect_and_read(client, immich)
    data = tmp_path / "data"
    other = "/library/Other.photoslibrary"
    folder_for(data, other).mkdir(parents=True)
    shutil.copy(data / "libraries" / library_id / "scan.json.gz", folder_for(data, other))
    library = Library(data, other)
    try:
        assert library.scan is None
    finally:
        library.close()


def test_damaged_saved_scan_is_ignored(tmp_path, main_library):
    data = tmp_path / "data"
    folder_for(data, main_library).mkdir(parents=True)
    (folder_for(data, main_library) / "scan.json.gz").write_bytes(b"not gzip")
    library = Library(data, main_library)
    try:
        assert library.scan is None
    finally:
        library.close()


def test_single_library_settings_are_upgraded(tmp_path, main_library):
    data = tmp_path / "data"
    data.mkdir()
    old = {
        "url": "http://x",
        "library": main_library,
        "autoMinutes": 15,
        "options": {"edits": "original"},
    }
    (data / "settings.json").write_text(json.dumps(old))
    state = State(data / "photobridge.db")
    state.save_item("uuid-1", "fingerprint", "done", "asset-1", {"main": "asset-1"})
    state.set_meta("last_sync", {"phase": "finished"})
    state.close()
    with gzip.open(data / "scan.json.gz", "wt") as file:
        json.dump({"library": "elsewhere"}, file)

    config = Config(data, None, "127.0.0.1", 0, "INFO", None, None)
    runtime = Runtime(config)
    try:
        [library] = runtime.libraries.values()
        assert library.path == main_library
        assert library.auto_minutes == 15 and library.options.edits == "original"
        assert library.state.counts() == {"done": 1}
        assert library.state.get_meta("last_sync") == {"phase": "finished"}
        assert runtime.settings["defaults"]["autoMinutes"] == 15
        assert not (data / "photobridge.db").exists() and not (data / "scan.json.gz").exists()
        saved = json.loads((data / "settings.json").read_text())
        assert "library" not in saved and saved["libraries"][0]["path"] == main_library
    finally:
        runtime.close()


def test_fresh_install_defaults_to_an_hourly_schedule(tmp_path):
    config = Config(tmp_path / "data", None, "127.0.0.1", 0, "INFO", None, None, str(tmp_path))
    with TestClient(create_app(config, background=False), base_url="http://testserver") as client:
        state = client.get("/api/state").json()
        assert state["libraries"] == []
        assert state["defaults"]["autoMinutes"] == 60


def test_library_is_chosen_from_the_shared_folder(client):
    listing = client.get("/api/folders").json()
    libraries = [f["path"] for f in listing["folders"] if f["library"]]
    assert "Test-Live-15.7.2.photoslibrary" in libraries
    response = client.post("/api/libraries", json={"path": "Test-Live-15.7.2.photoslibrary"})
    assert response.json()["path"] == "Test-Live-15.7.2.photoslibrary"
    assert response.json()["name"] == "Test-Live-15.7.2"
    assert client.post("/api/libraries", json={"path": ".."}).status_code == 422
    assert client.get("/api/folders", params={"path": "../.."}).status_code == 422


def leftover(tmp_path, name="osxphotos_abc", size=100):
    folder = tmp_path / "data" / "tmp" / name
    folder.mkdir(parents=True)
    (folder / "Photos.sqlite").write_bytes(b"x" * size)
    return folder


def test_a_read_leaves_no_database_copy(client, immich, tmp_path):
    connect_and_read(client, immich)
    assert client.get("/api/state").json()["leftovers"] is None
    assert not list((tmp_path / "data" / "tmp").iterdir())


def test_leftover_database_copies_are_found_and_deleted(client, tmp_path):
    empty = leftover(tmp_path, "osxphotos_empty", size=0)
    assert client.get("/api/state").json()["leftovers"] is None
    folder = leftover(tmp_path)
    other = tmp_path / "data" / "tmp" / "not-ours.txt"
    other.write_text("keep")
    assert client.get("/api/state").json()["leftovers"] == {"count": 2, "bytes": 100}
    assert client.post("/api/leftovers/delete", json={}).json() == {"count": 2, "bytes": 100}
    assert client.get("/api/state").json()["leftovers"] is None
    assert not folder.exists() and not empty.exists() and other.exists()


def test_leftovers_are_kept_while_a_library_is_read(client, tmp_path):
    folder = leftover(tmp_path)
    library = next(iter(client.app.state.runtime.libraries.values()))
    library.scan_running = True
    try:
        assert client.get("/api/state").json()["leftovers"] is None
        assert client.post("/api/leftovers/delete", json={}).status_code == 409
    finally:
        library.scan_running = False
    assert folder.exists()


def test_time_zone_is_chosen_in_settings(client, tmp_path):
    zones = client.get("/api/timezones").json()
    assert zones[0] == "UTC"
    assert "Europe/Lisbon" in zones
    assert "US/Eastern" not in zones
    assert client.get("/api/state").json()["timeZone"] == "UTC"
    assert client.post("/api/timezone", json={"timeZone": "Mars/Base"}).status_code == 422
    assert client.post("/api/timezone", json={"timeZone": "Europe/Lisbon"}).status_code == 200
    assert client.get("/api/state").json()["timeZone"] == "Europe/Lisbon"
    saved = json.loads((tmp_path / "data" / "settings.json").read_text())
    assert saved["timeZone"] == "Europe/Lisbon"
    assert timezones.now().tzinfo == ZoneInfo("Europe/Lisbon")


def test_time_zone_starts_from_tz_until_one_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    config = Config(tmp_path / "data", None, "127.0.0.1", 0, "INFO", None, None, str(tmp_path))
    with TestClient(create_app(config, background=False), base_url="http://testserver") as client:
        assert client.get("/api/state").json()["timeZone"] == "Asia/Tokyo"
    monkeypatch.setenv("TZ", "Not/AZone")
    (tmp_path / "data" / "settings.json").unlink()
    with TestClient(create_app(config, background=False), base_url="http://testserver") as client:
        assert client.get("/api/state").json()["timeZone"] == "UTC"


def test_log_times_use_the_chosen_time_zone():
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", None, None)
    record.created, record.msecs = 0.0, 0.0
    formatter = timezones.Formatter("%(asctime)s %(message)s")
    try:
        timezones.use("Asia/Tokyo")
        assert formatter.format(record) == "1970-01-01 09:00:00,000 hello"
    finally:
        timezones.use("UTC")
