from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from immigrate import timezones
from immigrate.config import Config
from immigrate.folder_library import ASSET_EXTENSIONS, clean_name, folder_marker, scan_folder
from immigrate.immich import Immich
from immigrate.libraries import Library
from immigrate.library import Scan
from immigrate.server import Runtime, create_app
from immigrate.state import State
from immigrate.sync import Job, Options, plan, verify

from . import mock_immich
from .mock_immich import Store, serve

WHEN = 1_500_000_000  # 2017-07-14 02:40:00 UTC


def add(path: Path, content: bytes | None = None, mtime: int = WHEN) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Immich tells files apart by checksum, so each one needs its own content.
    path.write_bytes(str(path).encode() if content is None else content)
    os.utime(path, (mtime, mtime))
    return path


def names(scan: Scan) -> list[str]:
    return sorted(item.filename for item in scan.items)


def by_name(scan: Scan) -> dict[str, object]:
    return {item.filename: item for item in scan.items}


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# Reading a folder


def test_only_photos_and_videos_become_items(tmp_path):
    add(tmp_path / "a.JPG")
    add(tmp_path / "trip" / "day1" / "b.heic")
    add(tmp_path / "trip" / "clip.MOV")
    add(tmp_path / "notes.txt")
    add(tmp_path / "trip" / "a.aae")
    add(tmp_path / "._a.JPG")
    add(tmp_path / ".DS_Store")
    scan = scan_folder(str(tmp_path))
    assert names(scan) == ["a.JPG", "b.heic", "clip.MOV"]
    assert scan.summary()["photos"] == 2 and scan.summary()["videos"] == 1
    assert all(not item.missing for item in scan.items)


@pytest.mark.parametrize(
    "folder",
    [
        "@eaDir",
        "@Recycle",
        "#recycle",
        "#snapshot",
        ".zfs",
        ".@__thumb",
        "$RECYCLE.BIN",
        "lost+found",
        "ix-applications",
        "Old.photoslibrary",
        "Older.PHOTOLIBRARY",
        "Aperture.aplibrary",
    ],
)
def test_system_folders_and_app_libraries_are_left_out(tmp_path, folder):
    add(tmp_path / "keep.jpg")
    add(tmp_path / folder / "thumbnail.jpg")
    add(tmp_path / "trip" / folder / "thumbnail.jpg")
    assert names(scan_folder(str(tmp_path))) == ["keep.jpg"]


def test_a_photos_library_without_its_suffix_is_left_out(tmp_path):
    add(tmp_path / "keep.jpg")
    add(tmp_path / "Renamed" / "database" / "Photos.sqlite")
    add(tmp_path / "Renamed" / "resources" / "derivatives" / "thumbnail.jpg")
    assert names(scan_folder(str(tmp_path))) == ["keep.jpg"]


def test_symlinks_are_not_followed(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    add(outside / "secret.jpg")
    add(tmp_path / "keep.jpg")
    (tmp_path / "linked-folder").symlink_to(outside)
    (tmp_path / "linked-file.jpg").symlink_to(outside / "secret.jpg")
    assert names(scan_folder(str(tmp_path))) == ["keep.jpg"]


def test_file_types_match_what_immich_accepts():
    assert ASSET_EXTENSIONS == mock_immich.ASSET_EXTENSIONS


def test_a_raw_and_a_photo_with_the_same_name_are_a_pair(tmp_path):
    add(tmp_path / "IMG_1.JPG")
    add(tmp_path / "img_1.cr2")
    add(tmp_path / "IMG_2.DNG")
    scan = scan_folder(str(tmp_path))
    items = by_name(scan)
    assert sorted(items) == ["IMG_1.JPG", "IMG_2.DNG"]
    assert [p.role for p in items["IMG_1.JPG"].parts] == ["main", "raw"]
    assert [p.role for p in items["IMG_2.DNG"].parts] == ["main"]
    assert scan.summary()["raw"] == 1


def test_only_a_clear_match_is_a_pair(tmp_path):
    add(tmp_path / "two" / "a.jpg")
    add(tmp_path / "two" / "a.png")
    add(tmp_path / "two" / "a.cr2")
    add(tmp_path / "apart" / "b.jpg")
    add(tmp_path / "elsewhere" / "b.cr2")
    add(tmp_path / "psd" / "c.jpg")
    add(tmp_path / "psd" / "c.psd")
    scan = scan_folder(str(tmp_path))
    assert len(scan.items) == 7 and scan.summary()["raw"] == 0


def test_a_video_is_never_treated_as_a_live_photo_by_its_name(tmp_path):
    add(tmp_path / "IMG_1.HEIC")
    add(tmp_path / "IMG_1.MOV")
    scan = scan_folder(str(tmp_path))
    assert names(scan) == ["IMG_1.HEIC", "IMG_1.MOV"]
    assert scan.summary()["live"] == 0
    assert by_name(scan)["IMG_1.MOV"].is_video


def test_times_come_from_the_file_and_ignore_the_chosen_time_zone(tmp_path):
    add(tmp_path / "a.jpg")
    scan = scan_folder(str(tmp_path))
    try:
        timezones.use("Asia/Tokyo")
        again = scan_folder(str(tmp_path))
    finally:
        timezones.use("UTC")
    assert scan.items[0].taken == datetime.fromtimestamp(WHEN, UTC)
    assert scan.items[0].taken.isoformat() == again.items[0].taken.isoformat()


def test_a_sidecar_beside_the_file_is_found(tmp_path):
    full = add(tmp_path / "a.jpg.xmp")
    add(tmp_path / "a.jpg")
    short = add(tmp_path / "b.XMP")
    add(tmp_path / "b.jpg")
    raw = add(tmp_path / "c.cr2.xmp")
    add(tmp_path / "c.jpg")
    add(tmp_path / "c.cr2")
    add(tmp_path / "d.jpg")
    add(tmp_path / "other" / "d.jpg.xmp")
    items = by_name(scan_folder(str(tmp_path)))
    assert items["a.jpg"].sidecar_path == str(full)
    assert items["b.jpg"].sidecar_path == str(short)
    assert items["c.jpg"].sidecar_path == str(raw)
    assert items["d.jpg"].sidecar_path == ""


def test_folders_with_files_give_the_album_names(tmp_path):
    root = tmp_path / "Photos"
    add(root / "top.jpg")
    add(root / "Trips" / "Lisbon" / "a.jpg")
    add(root / "Empty" / "notes.txt")
    scan = scan_folder(str(root))
    assert scan.album_names == ["Photos", "Photos/Trips/Lisbon"]
    assert by_name(scan)["a.jpg"].albums == ["Photos/Trips/Lisbon"]


def test_items_keep_their_ids_between_reads(tmp_path):
    add(tmp_path / "x" / "a.jpg")
    add(tmp_path / "y" / "a.jpg")
    first = scan_folder(str(tmp_path))
    second = scan_folder(str(tmp_path))
    assert [i.uuid for i in first.items] == [i.uuid for i in second.items]
    assert len({i.uuid for i in first.items}) == 2


def test_names_that_are_not_valid_utf8_can_be_sent():
    assert clean_name("caf\udce9.jpg") == "caf?.jpg"
    assert clean_name("café.jpg") == "café.jpg"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can open every folder")
def test_a_folder_that_cannot_be_opened_is_reported(tmp_path):
    add(tmp_path / "keep.jpg")
    locked = tmp_path / "trip" / "locked"
    add(locked / "a.jpg")
    locked.chmod(0)
    try:
        scan = scan_folder(str(tmp_path))
    finally:
        locked.chmod(0o755)
    assert names(scan) == ["keep.jpg"]
    assert scan.summary()["unreadable"] == 1
    assert scan.summary()["unreadableFolders"] == ["trip/locked"]


def test_a_missing_folder_is_an_error(tmp_path):
    with pytest.raises(OSError):
        scan_folder(str(tmp_path / "gone"))
    with pytest.raises(OSError):
        folder_marker(str(tmp_path / "gone"))


def test_saved_reads_load_again_including_older_ones(tmp_path):
    add(tmp_path / "a.jpg.xmp")
    add(tmp_path / "a.jpg")
    scan = scan_folder(str(tmp_path))
    data = scan.to_dict()
    assert Scan.from_dict(data).to_dict() == data
    del data["unreadable"]
    for item in data["items"]:
        del item["from_folder"], item["sidecar_path"]
    older = Scan.from_dict(data)
    assert older.unreadable == [] and not older.items[0].from_folder


def test_the_marker_changes_only_when_photos_or_videos_change(tmp_path):
    photo = add(tmp_path / "trip" / "a.jpg", b"same")
    marker = folder_marker(str(tmp_path))
    assert folder_marker(str(tmp_path)) == marker
    add(tmp_path / "notes.txt")
    add(tmp_path / "a.jpg.xmp")
    assert folder_marker(str(tmp_path)) == marker
    add(photo, b"same", mtime=WHEN - 100)  # an older time and the same size still counts
    changed = folder_marker(str(tmp_path))
    assert changed != marker
    added = add(tmp_path / "b.jpg", mtime=WHEN - 200)
    assert folder_marker(str(tmp_path)) != changed
    added.unlink()
    assert folder_marker(str(tmp_path)) == changed


# Sending a folder


@pytest.fixture
def world(tmp_path):
    store = Store()
    server = serve(store)
    state = State(tmp_path / "s.db")
    immich = Immich(f"http://127.0.0.1:{server.server_address[1]}", store.key, backoff=0.01)
    yield store, immich, state
    immich.close()
    state.close()
    server.shutdown()
    server.server_close()


@pytest.fixture
def folder(tmp_path):
    return tmp_path / "Photos"


def run(scan, state, immich, **options):
    job = Job(scan, state, immich, Options(**options))
    job.run()
    return job


def uploads(store):
    return sum(1 for call in store.calls if call == ("POST", "/api/assets"))


def test_no_sidecar_is_built_so_the_dates_in_the_file_are_kept(folder, world):
    store, immich, state = world
    add(folder / "a.jpg")
    scan = scan_folder(str(folder))
    job = run(scan, state, immich)
    assert job.phase == "finished" and not store.violations, store.violations
    [asset] = store.assets.values()
    assert asset["sidecar"] is None
    assert asset["fileCreatedAt"] == datetime.fromtimestamp(WHEN, UTC).isoformat()


def test_a_sidecar_beside_the_file_is_sent_unchanged(folder, world):
    store, immich, state = world
    content = b"<x:xmpmeta>mine</x:xmpmeta>"
    add(folder / "a.jpg.xmp", content)
    add(folder / "a.jpg")
    run(scan_folder(str(folder)), state, immich)
    [asset] = store.assets.values()
    assert asset["sidecar"] == content


def test_a_sidecar_that_vanished_is_a_failed_item_that_is_retried(folder, world):
    _, immich, state = world
    sidecar = add(folder / "a.jpg.xmp")
    add(folder / "a.jpg")
    add(folder / "b.jpg")
    scan = scan_folder(str(folder))
    sidecar.unlink()
    job = run(scan, state, immich)
    assert job.phase == "finished"
    assert job.status()["failed"] == 1 and job.status()["done"] == 1
    assert run(scan_folder(str(folder)), state, immich).status()["failed"] == 0


def test_raw_pairs_are_stacked(folder, world):
    store, immich, state = world
    add(folder / "a.jpg")
    add(folder / "a.cr2")
    scan = scan_folder(str(folder))
    run(scan, state, immich)
    ids = state.all_items()[scan.items[0].uuid]["asset_ids"]
    assert store.stacks == [[ids["main"], ids["raw"]]]


def test_syncing_leaves_the_folder_untouched(folder, world):
    _, immich, state = world
    add(folder / "a.jpg")
    add(folder / "a.cr2")
    add(folder / "a.jpg.xmp")
    add(folder / "trip" / "b.mov")
    before = snapshot(folder)
    run(scan_folder(str(folder)), state, immich, albums=True)
    assert snapshot(folder) == before


def test_a_second_run_sends_nothing(folder, world):
    store, immich, state = world
    add(folder / "a.jpg")
    run(scan_folder(str(folder)), state, immich)
    before = uploads(store)
    job = run(scan_folder(str(folder)), state, immich)
    assert uploads(store) == before and job.status()["skipped"] == 1


def test_a_moved_file_finds_its_existing_asset(folder, world):
    store, immich, state = world
    photo = add(folder / "a.jpg", b"same")
    run(scan_folder(str(folder)), state, immich)
    assert len(store.assets) == 1
    add(folder / "moved" / "a.jpg", b"same")
    photo.unlink()
    job = run(scan_folder(str(folder)), state, immich)
    assert job.status()["failed"] == 0 and len(store.assets) == 1


def test_a_changed_file_is_added_and_the_old_asset_stays(folder, world):
    store, immich, state = world
    photo = add(folder / "a.jpg", b"first")
    run(scan_folder(str(folder)), state, immich)
    add(photo, b"second", mtime=WHEN + 100)
    scan = scan_folder(str(folder))
    assert plan(scan, state, Options())["changed"] == 1
    run(scan, state, immich)
    assert len(store.assets) == 2


def test_a_removed_file_stays_in_immich(folder, world):
    store, immich, state = world
    photo = add(folder / "a.jpg")
    add(folder / "b.jpg")
    run(scan_folder(str(folder)), state, immich)
    photo.unlink()
    run(scan_folder(str(folder)), state, immich)
    assert len(store.assets) == 2
    assert not any(method == "DELETE" for method, _ in store.calls)


def test_albums_are_only_made_when_asked_for(folder, world):
    store, immich, state = world
    add(folder / "top.jpg")
    add(folder / "trip" / "a.jpg")
    add(folder / "trip" / "b.jpg")
    scan = scan_folder(str(folder))
    run(scan, state, immich, albums=False)
    assert store.albums == {}
    assert plan(scan, state, Options(albums=False))["newAlbums"] == 0
    assert plan(scan, state, Options(albums=True))["newAlbums"] == 2
    run(scan, state, immich, albums=True)
    counts = {a["albumName"]: len(a["assets"]) for a in store.albums.values()}
    assert counts == {"Photos": 1, "Photos/trip": 2}
    assert verify(scan, state, immich, Options(albums=True))["clean"]


def test_verify_finds_a_deleted_asset(folder, world):
    store, immich, state = world
    add(folder / "a.jpg")
    scan = scan_folder(str(folder))
    run(scan, state, immich)
    assert verify(scan, state, immich, Options())["clean"]
    store.assets.clear()
    assert verify(scan, state, immich, Options())["goneCount"] == 1


# Through the server


@pytest.fixture
def nas(tmp_path):
    root = tmp_path / "nas"
    add(root / "Photos" / "a.jpg")
    add(root / "Photos" / "trip" / "b.mov")
    add(root / "Photos" / "@eaDir" / "thumbnail.jpg")
    add(root / "@eaDir" / "thumbnail.jpg")
    add(root / "Real.photoslibrary" / "database" / "Photos.sqlite")
    add(root / "Real.photoslibrary" / "originals" / "a.jpg")
    add(root / "Empty.photoslibrary" / "notes.txt")
    return root


def config_for(tmp_path, nas):
    return Config(
        data_dir=tmp_path / "data",
        library_path=None,
        host="127.0.0.1",
        port=0,
        log_level="INFO",
        immich_url=None,
        immich_api_key=None,
        library_root=str(nas),
    )


@pytest.fixture
def client(tmp_path, nas):
    app = create_app(config_for(tmp_path, nas), background=False)
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


@pytest.fixture
def immich_server():
    store = Store()
    server = serve(store)
    yield store, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def wait_for(client, condition, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get("/api/state").json()
        if condition(state["libraries"][0]):
            return state["libraries"][0]
        time.sleep(0.02)
    raise AssertionError("Timed out")


def test_full_flow_over_http(client, immich_server):
    store, url = immich_server
    client.post("/api/connect", json={"url": url, "key": store.key})
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    assert library["kind"] == "folder" and library["name"] == "Photos"
    assert library["path"] == "Photos"
    assert library["options"]["albums"] is False, "a folder's albums have to be asked for"
    assert library["autoMinutes"] == 0
    url_for = f"/api/libraries/{library['id']}"
    assert client.post(f"{url_for}/read", json={}).status_code == 200
    library = wait_for(client, lambda lib: lib["scan"]["summary"])
    assert library["scan"]["summary"]["total"] == 2
    assert client.post(f"{url_for}/preview", json={"albums": False}).json()["new"] == 2
    assert client.post(f"{url_for}/sync", json={"read": False}).status_code == 200
    library = wait_for(client, lambda lib: lib["job"] and lib["job"]["phase"] == "finished")
    assert library["job"]["done"] == 2 and not store.violations
    report = client.post(f"{url_for}/verify").json()
    assert report["clean"] and store.albums == {}


def test_settings_with_only_a_schedule_and_albums_are_saved(client):
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    # What the folder's form sends: it keeps the other choices as they were.
    body = {"options": {**library["options"], "albums": True}, "autoMinutes": 60}
    saved = client.post(f"/api/libraries/{library['id']}/settings", json=body).json()
    assert saved["options"]["albums"] is True and saved["autoMinutes"] == 60


def test_the_browser_hides_system_folders_and_marks_libraries(client):
    listing = client.get("/api/folders").json()
    assert {f["name"]: f["library"] for f in listing["folders"]} == {
        "Empty.photoslibrary": True,
        "Photos": False,
        "Real.photoslibrary": True,
    }
    assert [f["name"] for f in client.get("/api/folders?path=Photos").json()["folders"]] == ["trip"]


def test_the_top_of_the_shared_folder_can_be_chosen(client, nas):
    library = client.post("/api/libraries", json={"path": ""}).json()
    assert library["kind"] == "folder" and library["path"] == ""
    assert library["name"] == nas.name


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("..", "outside the shared NAS folder"),
        ("nowhere", "can't open"),
        ("Photos/a.jpg", "can't open"),
        ("@eaDir", "doesn't read"),
        ("Photos/@eaDir", "doesn't read"),
        ("Real.photoslibrary/originals", "doesn't read"),
        ("Empty.photoslibrary", "no Photos database"),
        (None, "Choose a Photos library"),
        (7, "Choose a Photos library"),
    ],
)
def test_some_paths_can_not_be_chosen(client, path, message):
    response = client.post("/api/libraries", json={"path": path})
    assert response.status_code == 422 and message in response.json()["detail"]


def test_a_photos_library_is_still_added_as_one(client):
    library = client.post("/api/libraries", json={"path": "Real.photoslibrary"}).json()
    assert library["kind"] == "photos" and library["name"] == "Real"
    assert library["options"]["albums"] is True


def test_the_kind_survives_a_restart(client):
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    restarted = Runtime(client.app.state.runtime.config)
    try:
        assert restarted.libraries[library["id"]].kind == "folder"
    finally:
        restarted.close()


def test_libraries_saved_before_folders_existed_are_photos_libraries(tmp_path):
    library = Library(tmp_path / "data", "/library")
    try:
        assert library.kind == "photos" and library.to_settings()["kind"] == "photos"
    finally:
        library.close()


def test_a_read_of_a_folder_that_went_away_says_so(client, nas):
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    runtime = client.app.state.runtime
    runtime.libraries[library["id"]].path = str(nas / "gone")
    client.post(f"/api/libraries/{library['id']}/read", json={})
    library = wait_for(client, lambda lib: lib["scan"]["error"])
    assert "Couldn't read the library" in library["scan"]["error"]


def test_finding_items_is_logged_without_a_total(tmp_path, caplog):
    library = Library(tmp_path / "data", str(tmp_path), kind="folder")
    caplog.set_level(logging.INFO, logger="immigrate.libraries")
    try:
        library.report_scan(50, 0)
    finally:
        library.close()
    assert [r.getMessage() for r in caplog.records] == ["Found 50 items so far"]


def test_the_schedule_syncs_a_folder_only_after_it_changes(client, nas, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.update(url="http://x", key="k")
    added = client.post("/api/libraries", json={"path": "Photos"}).json()
    library = runtime.libraries[added["id"]]
    library.auto_minutes = 15
    synced = []
    monkeypatch.setattr(runtime, "read_and_sync", synced.append)
    runtime.auto_sync_once(now=10_000.0)
    assert synced == [library], "a folder that has never synced counts as changed"
    library.state.set_meta("synced_mtime", library.change_marker())
    runtime.auto_sync_once(now=20_000.0)
    assert len(synced) == 1 and library.last_check == 20_000.0
    add(nas / "Photos" / "new.jpg", mtime=WHEN - 500)
    runtime.auto_sync_once(now=30_000.0)
    assert len(synced) == 2


def test_the_schedule_skips_a_folder_it_can_not_read(client, nas, monkeypatch):
    runtime = client.app.state.runtime
    runtime.settings.update(url="http://x", key="k")
    added = client.post("/api/libraries", json={"path": "Photos"}).json()
    library = runtime.libraries[added["id"]]
    library.auto_minutes = 15
    library.path = str(nas / "gone")
    synced = []
    monkeypatch.setattr(runtime, "read_and_sync", synced.append)
    runtime.auto_sync_once(now=10_000.0)
    assert synced == [] and library.last_check == 10_000.0


def test_a_scheduled_sync_records_the_folder_as_synced(client, immich_server):
    store, url = immich_server
    client.post("/api/connect", json={"url": url, "key": store.key})
    added = client.post("/api/libraries", json={"path": "Photos"}).json()
    runtime = client.app.state.runtime
    library = runtime.libraries[added["id"]]
    library.auto_minutes = 15
    runtime.auto_sync_once(now=10_000.0)
    wait_for(client, lambda lib: lib["job"] and lib["job"]["phase"] == "finished")
    # The record is written right after the job's last status change.
    deadline = time.time() + 5
    while library.state.get_meta("synced_mtime") is None and time.time() < deadline:
        time.sleep(0.02)
    assert library.state.get_meta("synced_mtime") == library.change_marker()
    before = uploads(store)
    runtime.auto_sync_once(now=20_000.0)
    assert uploads(store) == before
