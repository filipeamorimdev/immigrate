import copy
import re
import xml.etree.ElementTree as ET

import pytest

from immigrate import sync as sync_module
from immigrate.immich import Immich
from immigrate.state import State
from immigrate.sync import Job, Options, plan, verify
from immigrate.xmp import build_sidecar

from .mock_immich import Store, serve


@pytest.fixture
def world(tmp_path):
    store = Store()
    server = serve(store)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    state = State(tmp_path / "s.db")
    immich = Immich(url, store.key, backoff=0.01)
    yield store, immich, state
    immich.close()
    state.close()
    server.shutdown()
    server.server_close()


def run(scan, state, immich, **options):
    job = Job(scan, state, immich, Options(**options))
    job.run()
    return job


def uploads(store):
    return sum(1 for call in store.calls if call == ("POST", "/api/assets"))


def test_scan_reads_macos_26_library(scan):
    s = scan.summary()
    assert s["total"] == 14 and s["missing"] == 2 and s["albums"] == 7
    assert s["edited"] == 4 and s["raw"] >= 1


def test_missing_items_say_why_and_where(scan):
    s = scan.summary()
    assert s["missingReferenced"] == 2 and s["missingInIcloud"] == 0
    missing = {m["filename"]: m for m in scan.missing_items()}
    assert set(missing) == {"IMG_2000.JPG", "Pumpkins4.jpg"}
    assert {m["reason"] for m in missing.values()} == {"referenced"}
    assert missing["IMG_2000.JPG"]["path"] == "/Users/rhet/Downloads/IMG_2000.JPG"


def test_full_sync_sends_everything_and_follows_the_contract(scan, world):
    store, immich, state = world
    job = run(scan, state, immich)
    status = job.status()
    assert job.phase == "finished" and not store.violations, store.violations
    hidden = scan.summary()["hidden"]
    missing_and_visible = sum(1 for i in scan.items if i.missing and not i.hidden)
    assert status["failed"] == 0 and status["missing"] == missing_and_visible
    assert status["excluded"] == hidden
    assert status["done"] == 14 - hidden - missing_and_visible
    assert store.stacks, "edits and RAW pairs should be stacked"
    assert len(store.albums) >= 1


def test_every_part_is_uploaded_with_its_metadata(scan, world):
    store, immich, state = world
    run(scan, state, immich)
    records = state.all_items()
    for item in scan.items:
        if item.missing or item.hidden:
            continue
        record = records[item.uuid]
        roles = {p.role for p in item.parts}
        assert set(record["asset_ids"]) == roles
        asset = store.assets[record["asset_ids"]["main"]]
        assert asset["isFavorite"] == item.favorite
        assert asset["sidecar"]


def test_album_membership_matches_apple_photos(scan, world):
    store, immich, state = world
    run(scan, state, immich)
    records = state.all_items()
    albums = {a["albumName"]: a for a in store.albums.values()}
    for title in {n for i in scan.items if not i.hidden and not i.missing for n in i.albums}:
        expected = {
            records[i.uuid]["primary_id"]
            for i in scan.items
            if title in i.albums and not i.hidden and not i.missing
        }
        assert set(albums[title]["assets"]) == expected


def test_live_photo_video_is_hidden_and_linked(live_scan, world):
    store, immich, state = world
    assert run(live_scan, state, immich).phase == "finished" and not store.violations
    item = next(i for i in live_scan.items if i.part("live"))
    ids = state.all_items()[item.uuid]["asset_ids"]
    assert store.assets[ids["live"]]["visibility"] == "hidden"
    assert store.assets[ids["main"]]["livePhotoVideoId"] == ids["live"]


def test_second_run_sends_nothing(scan, world):
    store, immich, state = world
    run(scan, state, immich)
    before = uploads(store)
    job = run(scan, state, immich)
    assert uploads(store) == before and job.status()["done"] == 0


def test_plan_is_accurate_before_and_after(scan, world):
    _, immich, state = world
    options = Options()
    first = plan(scan, state, options)
    assert first["unchanged"] == 0 and first["new"] > 0 and first["bytes"] > 0
    run(scan, state, immich)
    second = plan(scan, state, options)
    assert second["new"] == 0 and second["changed"] == 0 and second["bytes"] == 0
    assert second["unchanged"] == first["new"]


def test_stop_then_resume_finishes_without_resending(scan, world):
    store, immich, state = world
    job = Job(scan, state, immich, Options(workers=1))
    original = job.sync_item

    def stop_after_three(item):
        original(item)
        if job.status()["done"] >= 3:
            job.stop_event.set()

    job.sync_item = stop_after_three
    job.run()
    assert job.phase == "stopped"
    sent = job.status()["done"]
    assert 3 <= sent < 10
    resumed = run(scan, state, immich)
    assert resumed.phase == "finished" and resumed.status()["skipped"] == sent
    assert not store.violations


def test_lost_state_reconnects_to_existing_assets(scan, world, tmp_path):
    store, immich, state = world
    run(scan, state, immich)
    albums_before = {k: list(v["assets"]) for k, v in store.albums.items()}
    count_before = len(store.assets)
    fresh = State(tmp_path / "fresh.db")
    job = run(scan, fresh, immich)
    fresh.close()
    assert job.phase == "finished" and job.status()["failed"] == 0 and not store.violations
    assert not job.warnings, "favorites and Live Photo links reach the existing copies"
    assert len(store.assets) == count_before, "no duplicate assets"
    assert {k: list(v["assets"]) for k, v in store.albums.items()} == albums_before, (
        "no duplicate albums"
    )


def test_transient_errors_are_retried(scan, world):
    store, immich, state = world
    store.fail_uploads = 2
    job = run(scan, state, immich)
    assert job.status()["failed"] == 0


def test_a_failed_photo_is_reported_and_retried_later(scan, world):
    store, immich, state = world
    victim = next(i for i in scan.items if not i.missing and not i.hidden and not i.albums)
    store.fail_filenames = {victim.part("main").filename}
    job = run(scan, state, immich)
    assert job.phase == "finished" and job.status()["failed"] >= 1
    assert state.all_items()[victim.uuid]["status"] == "failed"
    store.fail_filenames = set()
    assert plan(scan, state, Options())["retry"] >= 1
    assert run(scan, state, immich).status()["failed"] == 0
    assert state.all_items()[victim.uuid]["status"] == "done"


def test_server_outage_stops_the_run(scan, world, monkeypatch):
    store, immich, state = world
    store.fail_filenames = {p.filename for i in scan.items for p in i.parts}
    monkeypatch.setattr(sync_module, "STOP_AFTER_FAILURES_IN_A_ROW", 2)
    job = run(scan, state, immich, workers=1)
    assert job.phase == "failed" and "failures in a row" in job.error


def test_verify_clean_then_detects_a_deleted_asset(scan, world):
    store, immich, state = world
    options = Options()
    run(scan, state, immich)
    report = verify(scan, state, immich, options)
    assert report["clean"], report
    primary = next(r["primary_id"] for r in state.all_items().values())
    del store.assets[primary]
    for album in store.albums.values():
        album["assets"] = [a for a in album["assets"] if a != primary]
    report = verify(scan, state, immich, options)
    assert not report["clean"] and report["goneCount"] == 1


def test_originals_only_sends_no_edits(scan, world):
    _, immich, state = world
    run(scan, state, immich, edits="original")
    assert not any(r["asset_ids"].get("edit") for r in state.all_items().values())


def test_hidden_archive_option(scan, world):
    store, immich, state = world
    # The sample's only hidden photo has no original on disk, so hide one that does.
    scan = copy.copy(scan)
    scan.items = [copy.copy(i) for i in scan.items]
    next(i for i in scan.items if not i.missing).hidden = True
    run(scan, state, immich, hidden="archive")
    assert any(a["visibility"] == "archive" for a in store.assets.values())
    assert verify(scan, state, immich, Options(hidden="archive"))["clean"]


def test_sidecar_is_valid_xml_with_escaping_and_southern_gps(scan):
    item = next(i for i in scan.items if i.keywords)
    item = copy.copy(item)
    item.title, item.description = "Tom & Jerry <3", 'a "quoted" note'
    item.latitude, item.longitude = -33.8688, -151.2093
    item.people = ["Ana Sá"]
    xml = build_sidecar(item, True)
    ET.fromstring(xml)  # noqa: S314 - our own output
    text = xml.decode()
    assert "33,52.128000S" in text and "151,12.558000W" in text
    assert "People/Ana Sá" in text and "Tom &amp; Jerry &lt;3" in text
    assert re.search(r"<exif:DateTimeOriginal>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d<", text)
