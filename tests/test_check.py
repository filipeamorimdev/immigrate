from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from immigrate import check as check_module
from immigrate import metadata
from immigrate.check import Check, run_check
from immigrate.config import Config
from immigrate.findings import findings
from immigrate.folder_library import scan_folder
from immigrate.immich import Immich
from immigrate.server import create_app
from immigrate.state import State
from immigrate.sync import Job, Options, plan, verify

from .mock_immich import Store, serve

WHEN = 1_500_000_000
ROOT = Path(__file__).resolve().parent.parent
SAMPLE_JPEGS = sorted((ROOT / ".samples").glob("tests/*/originals/*/*.jpeg"))
needs_exiftool = pytest.mark.skipif(not metadata.available(), reason="exiftool isn't installed")


def add(path: Path, content: bytes | None = None, mtime: int = WHEN) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(str(path).encode() if content is None else content)
    os.utime(path, (mtime, mtime))
    return path


def details(date=None, latitude=None, longitude=None):
    return {"date": date, "latitude": latitude, "longitude": longitude}


@pytest.fixture
def fake_exiftool(monkeypatch):
    """Dates and locations by file name, instead of running exiftool.

    Like exiftool, a file with no tags gets an empty record. A name set to None is a
    file exiftool can't read.
    """
    known: dict[str, dict | None] = {}
    reads: list[str] = []

    def read(paths):
        reads.extend(paths)
        found = {p: known.get(os.path.basename(p), details()) for p in paths}
        return {p: value for p, value in found.items() if value is not None}

    monkeypatch.setattr(metadata, "available", lambda: True)
    monkeypatch.setattr(metadata, "read", read)
    return known, reads


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


def check_folder(folder, state, immich=None, stop=None):
    scan = scan_folder(str(folder))
    return scan, run_check(scan, state, immich, True, stop or threading.Event())


def by_name(scan):
    return {item.filename: item for item in scan.items}


def uploads(store):
    return sum(1 for call in store.calls if call == ("POST", "/api/assets"))


# Reading dates and locations


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2019:03:14 10:00:00", "2019-03-14T10:00:00"),
        ("2019:03:14 10:00:00+01:00", "2019-03-14T10:00:00+01:00"),
        ("2019:03:14 10:00:00.123Z", "2019-03-14T10:00:00.123+00:00"),
        ("2019:03:14 10:00", "2019-03-14T10:00:00"),
        ("0000:00:00 00:00:00", None),
        ("2019:13:01 10:00:00", None),
        ("not a date", None),
        (20190314, None),
    ],
)
def test_dates_are_read_like_immich_reads_them(value, expected):
    assert metadata.parse_date(value) == expected


def test_the_first_date_tag_immich_uses_wins():
    record = {
        "QuickTime:CreateDate": "2001:01:01 00:00:00",
        "EXIF:DateTimeOriginal": "2019:03:14 10:00:00",
    }
    assert metadata.parse_record(record)["date"] == "2019-03-14T10:00:00"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (
            {
                "Composite:GPSLatitude": -33.86,
                "Composite:GPSLongitude": 151.2,
                "EXIF:GPSLatitude": 33.86,
            },
            (-33.86, 151.2),
        ),
        (
            {
                "EXIF:GPSLatitude": 33.86,
                "EXIF:GPSLatitudeRef": "S",
                "EXIF:GPSLongitude": 9.1,
                "EXIF:GPSLongitudeRef": "W",
            },
            (-33.86, -9.1),
        ),
        ({"XMP:GPSLatitude": 38.7, "XMP:GPSLongitude": -9.1}, (38.7, -9.1)),
        ({"QuickTime:GPSCoordinates": "38.7 -9.1 12"}, (38.7, -9.1)),
        ({"Composite:GPSLatitude": 0, "Composite:GPSLongitude": 0}, (None, None)),
        ({"Composite:GPSLatitude": 95, "Composite:GPSLongitude": 0}, (None, None)),
        ({"Composite:GPSLatitude": 38.7}, (None, None)),
        ({}, (None, None)),
    ],
)
def test_locations_keep_their_hemisphere(record, expected):
    found = metadata.parse_record(record)
    assert (found["latitude"], found["longitude"]) == expected


def test_files_exiftool_could_not_read_are_unknown_not_missing(monkeypatch, tmp_path):
    good, bad = str(tmp_path / "a.jpg"), str(tmp_path / "b.jpg")
    output = [
        {"SourceFile": good, "EXIF:DateTimeOriginal": "2019:03:14 10:00:00"},
        {"SourceFile": bad, "ExifTool:Error": "File format error"},
    ]
    sent = []

    def run(arguments, input, **_):
        sent.append(input)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(output).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    found = metadata.read([good, bad, str(tmp_path / "line\nbreak.jpg")])
    assert found == {good: details("2019-03-14T10:00:00")}
    assert b"line" not in sent[0]


def test_a_failing_exiftool_is_logged_not_raised(monkeypatch, tmp_path):
    def run(*_, **__):
        raise subprocess.TimeoutExpired("exiftool", 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert metadata.read([str(tmp_path / "a.jpg")]) == {}


@needs_exiftool
@pytest.mark.skipif(not SAMPLE_JPEGS, reason="run scripts/fetch-sample-libraries.sh first")
def test_real_exiftool_reads_dates_locations_and_sidecars(tmp_path):
    plain = tmp_path / "plain.jpg"
    shutil.copy(SAMPLE_JPEGS[0], plain)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-all=", str(plain)], check=True)
    tagged = tmp_path / "tagged.jpg"
    shutil.copy(plain, tagged)
    subprocess.run(
        [
            "exiftool",
            "-q",
            "-overwrite_original",
            "-DateTimeOriginal=2019:03:14 10:00:00",
            "-GPSLatitude=33.86",
            "-GPSLatitudeRef=S",
            "-GPSLongitude=151.2",
            "-GPSLongitudeRef=E",
            str(tagged),
        ],
        check=True,
    )
    sidecar = tmp_path / "plain.jpg.xmp"
    sidecar.write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:exif="http://ns.adobe.com/exif/1.0/">'
        "<exif:DateTimeOriginal>2020-01-02T03:04:05</exif:DateTimeOriginal>"
        "<exif:GPSLatitude>38,43.338N</exif:GPSLatitude><exif:GPSLongitude>9,8.4W</exif:GPSLongitude>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    found = metadata.read([str(plain), str(tagged), str(sidecar)])
    assert found[str(plain)] == details()
    assert found[str(tagged)]["date"] == "2019-03-14T10:00:00"
    assert found[str(tagged)]["latitude"] == pytest.approx(-33.86)
    assert found[str(sidecar)]["date"].startswith("2020-01-02T03:04:05")
    assert found[str(sidecar)]["longitude"] == pytest.approx(-9.14)


# The check


def test_identical_files_are_copies_and_the_one_with_a_sidecar_is_kept(tmp_path, fake_exiftool):
    add(tmp_path / "a" / "one.jpg", b"same")
    add(tmp_path / "b" / "two.jpg", b"same")
    add(tmp_path / "b" / "two.jpg.xmp")
    add(tmp_path / "c" / "three.jpg", b"different")
    state = State(tmp_path / "s.db")
    scan, result = check_folder(tmp_path, state)
    items = by_name(scan)
    assert result.copies == {items["one.jpg"].uuid: items["two.jpg"].uuid}
    assert result.complete and not result.immich_asked
    state.close()


def test_a_second_check_only_reads_changed_files(tmp_path, fake_exiftool, monkeypatch):
    _, reads = fake_exiftool
    add(tmp_path / "a.jpg")
    changing = add(tmp_path / "b.jpg", b"first")
    state = State(tmp_path / "s.db")
    check_folder(tmp_path, state)
    hashed = []
    original = check_module.sha1_of
    monkeypatch.setattr(
        check_module, "sha1_of", lambda p, s=None: hashed.append(p) or original(p, s)
    )
    reads.clear()
    check_folder(tmp_path, state)
    assert hashed == [] and reads == []
    add(changing, b"second!", mtime=WHEN + 5)
    _, result = check_folder(tmp_path, state)
    assert hashed == [str(changing)] and reads == [str(changing)]
    assert result.checksum(str(changing)) == check_module.sha1_of(str(changing))
    state.close()


def test_missing_dates_and_locations_are_found_and_sidecars_count(tmp_path, fake_exiftool):
    known, _ = fake_exiftool
    known["full.jpg"] = details("2019-03-14T10:00:00", 38.7, -9.1)
    known["nothing.jpg"] = details()
    known["sidecar.jpg"] = details()
    known["sidecar.jpg.xmp"] = details("2020-01-01T00:00:00", 41.1, -8.6)
    known["unknown.jpg"] = None
    for name in ("full.jpg", "nothing.jpg", "sidecar.jpg", "sidecar.jpg.xmp", "unknown.jpg"):
        add(tmp_path / name)
    state = State(tmp_path / "s.db")
    scan, result = check_folder(tmp_path, state)
    items = by_name(scan)
    assert result.details_read
    assert result.missing_date == [items["nothing.jpg"].uuid]
    assert result.missing_location == [items["nothing.jpg"].uuid]
    assert result.places[items["sidecar.jpg"].uuid] == [41.1, -8.6]
    assert result.dates[items["full.jpg"].uuid] == "2019-03-14T10:00:00"
    state.close()


def test_without_exiftool_nothing_is_reported_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(metadata, "available", lambda: False)
    add(tmp_path / "a.jpg")
    state = State(tmp_path / "s.db")
    _, result = check_folder(tmp_path, state)
    assert not result.details_read and result.missing_date == [] and result.missing_location == []
    state.close()


def test_a_stopped_check_is_incomplete_and_keeps_what_it_read(tmp_path, fake_exiftool):
    add(tmp_path / "a.jpg")
    state = State(tmp_path / "s.db")
    stop = threading.Event()
    stop.set()
    _, result = check_folder(tmp_path, state, stop=stop)
    assert not result.complete and result.checked_at
    state.close()


def test_an_unreadable_file_is_reported(tmp_path, fake_exiftool):
    add(tmp_path / "a.jpg")
    gone = add(tmp_path / "b.jpg")
    state = State(tmp_path / "s.db")
    scan = scan_folder(str(tmp_path))
    gone.unlink()
    result = run_check(scan, state, None, True, threading.Event())
    assert result.unreadable == ["b.jpg"] and result.summary()["unreadable"] == 1
    state.close()


def test_immich_is_asked_what_it_already_has(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    phone = add(folder / "from-phone.jpg", b"phone")
    trashed = add(folder / "trashed.jpg", b"trash")
    add(folder / "new.jpg", b"new")
    for path in (phone, trashed):
        immich.upload(
            str(path), path.name, "2019-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00"
        )
    store.trashed.add(store.by_checksum[check_module.sha1_of(str(trashed))])
    scan, result = check_folder(folder, state, immich)
    items = by_name(scan)
    assert result.immich_asked and len(result.in_immich) == 2
    assert (
        result.existing_asset(items["from-phone.jpg"])
        == store.by_checksum[check_module.sha1_of(str(phone))]
    )
    assert result.existing_asset(items["new.jpg"]) is None
    assert len(result.trashed) == 1 and not store.violations


def test_a_photos_library_can_only_miss_a_location(scan, tmp_path):
    state = State(tmp_path / "s.db")
    result = run_check(scan, state, None, False, threading.Event())
    present = [i for i in scan.items if not i.missing]
    assert result.complete and result.missing_date == []
    without = {i.uuid for i in present if i.latitude is None and i.uuid not in result.copies}
    assert set(result.missing_location) == without
    assert all(result.checksum(p.path) for i in present for p in i.parts)
    gaps = findings(scan, result, state, "photos", scan.library)["gaps"]
    assert {g["where"] for g in gaps} <= {i.filename for i in present}, "names, not paths inside"
    state.close()


def test_a_check_survives_being_saved_and_loaded():
    result = Check(
        library="/x", scanned_at="now", copies={"a": "b"}, files={"/x/a.jpg": [1, 2, "f"]}
    )
    assert Check.from_dict({**result.to_dict(), "added_later": 1}) == result


# Syncing with a check


def test_items_immich_already_has_are_linked_without_uploading(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    phone = add(folder / "trip" / "a.jpg", b"phone")
    add(folder / "trip" / "b.jpg", b"new")
    immich.upload(str(phone), "a.jpg", "2019-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00")
    before = uploads(store)
    scan, result = check_folder(folder, state, immich)
    preview = plan(scan, state, Options(albums=True), result)
    assert preview["linked"] == 1 and preview["existing"] == 1 and preview["bytes"] == len(b"new")
    job = Job(scan, state, immich, Options(albums=True), result)
    job.run()
    assert uploads(store) == before + 1
    status = job.status()
    assert status["done"] == 1 and status["skipped"] == 1 and status["bytesDone"] == len(b"new")
    assert status["bytesTotal"] == status["bytesSent"] == len(b"new")
    [album] = store.albums.values()
    assert len(album["assets"]) == 2, "the linked item joins the album too"
    assert verify(scan, state, immich, Options(albums=True), result)["clean"]


def test_items_immich_already_has_can_be_left_alone(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    phone = add(folder / "a.jpg", b"phone")
    immich.upload(str(phone), "a.jpg", "2019-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00")
    scan, result = check_folder(folder, state, immich)
    options = Options(albums=True, existing="skip")
    assert plan(scan, state, options, result)["excluded"] == 1
    job = Job(scan, state, immich, options, result)
    job.run()
    assert job.status()["excluded"] == 1 and store.albums == {}
    assert verify(scan, state, immich, options, result)["expected"] == 0


def test_copies_upload_once_and_join_their_own_albums(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    add(folder / "one" / "a.jpg", b"same")
    add(folder / "two" / "a-copy.jpg", b"same")
    scan, result = check_folder(folder, state, immich)
    assert plan(scan, state, Options(), result)["bytes"] == len(b"same")
    job = Job(scan, state, immich, Options(albums=True), result)
    job.run()
    assert uploads(store) == 1 and len(store.assets) == 1
    # The copy counted toward the total until it was linked instead of uploaded.
    assert job.status()["bytesTotal"] == job.status()["bytesSent"] == len(b"same")
    assert {a["albumName"]: len(a["assets"]) for a in store.albums.values()} == {
        "Photos/one": 1,
        "Photos/two": 1,
    }


def test_copies_can_be_left_out(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    add(folder / "one" / "a.jpg", b"same")
    add(folder / "two" / "a-copy.jpg", b"same")
    scan, result = check_folder(folder, state, immich)
    job = Job(scan, state, immich, Options(albums=True, copies="skip"), result)
    job.run()
    assert job.status()["excluded"] == 1 and len(store.albums) == 1


def test_a_file_changed_after_the_check_is_uploaded_not_linked(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    photo = add(folder / "a.jpg", b"first")
    immich.upload(str(photo), "a.jpg", "2019-01-01T00:00:00+00:00", "2019-01-01T00:00:00+00:00")
    scan, result = check_folder(folder, state, immich)
    add(photo, b"second", mtime=WHEN + 10)
    Job(scan, state, immich, Options(), result).run()
    assert len(store.assets) == 2


def test_dates_and_locations_that_were_set_go_to_immich_once(tmp_path, world, fake_exiftool):
    store, immich, state = world
    folder = tmp_path / "Photos"
    add(folder / "a.jpg")
    add(folder / "b.jpg")
    add(folder / "c.jpg")
    scan, result = check_folder(folder, state, immich)
    items = by_name(scan)
    a, b, c = items["a.jpg"].uuid, items["b.jpg"].uuid, items["c.jpg"].uuid
    state.save_fixes([a, b], latitude=38.7, longitude=-9.1, place="Lisbon")
    state.save_fixes([b], date="2019-03-14T10:00:00+00:00")
    Job(scan, state, immich, Options(), result).run()
    assert sorted(len(u["ids"]) for u in store.updates) == [1, 1]
    assert {frozenset(u) for u in store.updates} == {
        frozenset({"ids", "latitude", "longitude"}),
        frozenset({"ids", "dateTimeOriginal", "latitude", "longitude"}),
    }
    assert not store.violations
    Job(scan, state, immich, Options(), result).run()
    assert len(store.updates) == 2, "nothing is sent again"
    state.save_fixes([c], date="2018-01-01T00:00:00+00:00")
    state.save_fixes([a], latitude=41.1, longitude=-8.6, place="Porto")
    Job(scan, state, immich, Options(), result).run()
    assert len(store.updates) == 4
    record = state.all_items()[a]
    assert store.assets[record["primary_id"]]["latitude"] == 41.1


def test_a_cleared_fix_is_forgotten(tmp_path):
    state = State(tmp_path / "s.db")
    state.save_fixes(["a", "b"], latitude=1.0, longitude=2.0, place="X")
    state.save_fixes(["a"], date="2019-01-01T00:00:00+00:00")
    state.save_fixes(["a", "b"], latitude=None, longitude=None, place=None)
    fixes = state.fixes()
    assert set(fixes) == {"a"} and fixes["a"]["date"] and fixes["a"]["latitude"] is None
    state.close()


# What the page sees


def test_findings_suggest_the_nearest_location_in_the_same_folder(tmp_path, fake_exiftool):
    known, _ = fake_exiftool
    known["near.jpg"] = details("2019-03-14T10:05:00", 38.7, -9.1)
    known["far.jpg"] = details("2019-03-14T18:00:00", 41.1, -8.6)
    known["lost.jpg"] = details("2019-03-14T10:00:00")
    known["elsewhere.jpg"] = details("2019-03-14T10:00:00", 1.0, 1.0)
    for name in ("near.jpg", "far.jpg", "lost.jpg"):
        add(tmp_path / "trip" / name)
    add(tmp_path / "other" / "elsewhere.jpg")
    state = State(tmp_path / "s.db")
    scan, result = check_folder(tmp_path, state)
    data = findings(scan, result, state, "folder", str(tmp_path))
    [gap] = data["gaps"]
    assert gap["where"] == "trip/lost.jpg" and gap["group"].endswith("/trip")
    assert gap["missingLocation"] and not gap["missingDate"]
    assert gap["suggestion"] == {
        "latitude": 38.7,
        "longitude": -9.1,
        "from": "near.jpg",
        "minutes": 5,
    }
    state.close()


def test_no_location_is_suggested_without_a_capture_date_or_from_days_away(tmp_path, fake_exiftool):
    known, _ = fake_exiftool
    known["placed.jpg"] = details("2019-03-14T10:00:00", 38.7, -9.1)
    known["later.jpg"] = details("2019-03-20T10:00:00")
    for name in ("placed.jpg", "undated.jpg", "later.jpg"):
        add(tmp_path / name)
    state = State(tmp_path / "s.db")
    scan, result = check_folder(tmp_path, state)
    gaps = findings(scan, result, state, "folder", str(tmp_path))["gaps"]
    assert {g["filename"]: g["suggestion"] for g in gaps} == {
        "undated.jpg": None,
        "later.jpg": None,
    }
    state.close()


# Through the server


@pytest.fixture
def client(tmp_path, fake_exiftool):
    known, _ = fake_exiftool
    known["dated.jpg"] = details("2019-03-14T10:00:00", 38.7, -9.1)
    nas = tmp_path / "nas"
    add(nas / "Photos" / "dated.jpg")
    add(nas / "Photos" / "undated.jpg")
    config = Config(tmp_path / "data", None, "127.0.0.1", 0, "INFO", None, None, str(nas))
    with TestClient(
        create_app(config, background=False), base_url="http://testserver"
    ) as test_client:
        yield test_client


@pytest.fixture
def immich_server():
    store = Store()
    server = serve(store)
    yield store, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def settle(client, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        library = client.get("/api/state").json()["libraries"][0]
        if library["scan"]["summary"] and not library["scan"]["running"]:
            return library
        time.sleep(0.02)
    raise AssertionError("Timed out")


def added(client):
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    client.post(f"/api/libraries/{library['id']}/read", json={})
    return settle(client)


def test_a_read_is_followed_by_a_check(client, immich_server):
    store, url = immich_server
    client.post("/api/connect", json={"url": url, "key": store.key})
    library = added(client)
    check = library["scan"]["check"]
    assert check["complete"] and check["immichAsked"] and check["missingDate"] == 1
    data = client.get(f"/api/libraries/{library['id']}/findings").json()
    [gap] = data["gaps"]
    assert gap["filename"] == "undated.jpg" and gap["missingDate"] and gap["missingLocation"]
    assert gap["suggestion"] is None, "no capture date, so no time to match on"


def test_without_a_connection_the_check_skips_immich(client):
    library = added(client)
    assert library["scan"]["check"]["complete"] and not library["scan"]["check"]["immichAsked"]
    assert client.post(f"/api/libraries/{library['id']}/check", json={}).json() == {"started": True}
    assert settle(client)["scan"]["check"]


def test_findings_need_a_check(client):
    library = client.post("/api/libraries", json={"path": "Photos"}).json()
    assert client.get(f"/api/libraries/{library['id']}/findings").status_code == 409
    assert client.post(f"/api/libraries/{library['id']}/check", json={}).status_code == 409


def test_fixes_are_saved_and_checked(client):
    library = added(client)
    url = f"/api/libraries/{library['id']}/fixes"
    gap = client.get(f"/api/libraries/{library['id']}/findings").json()["gaps"][0]
    uuids = [gap["uuid"]]
    bad = [
        {"uuids": ["nope"], "date": "2019-01-01T10:00"},
        {"uuids": [], "date": "2019-01-01T10:00"},
        {"uuids": uuids},
        {"uuids": uuids, "date": "yesterday"},
        {"uuids": uuids, "date": "1700-01-01T10:00"},
        {"uuids": uuids, "latitude": 91, "longitude": 0},
        {"uuids": uuids, "latitude": 10},
        {"uuids": uuids, "latitude": "north", "longitude": 1},
    ]
    for body in bad:
        assert client.post(url, json=body).status_code == 422, body
    client.post("/api/timezone", json={"timeZone": "Europe/Lisbon"})
    assert client.post(url, json={"uuids": uuids, "date": "2019-07-01T10:00"}).json() == {
        "saved": 1
    }
    assert (
        client.post(
            url, json={"uuids": uuids, "latitude": 0, "longitude": 0, "place": "Null Island"}
        ).status_code
        == 200
    )
    gap = client.get(f"/api/libraries/{library['id']}/findings").json()["gaps"][0]
    assert gap["fix"]["date"] == "2019-07-01T10:00:00+01:00", "typed in the chosen time zone"
    assert gap["fix"]["place"] == "Null Island" and gap["fix"]["latitude"] == 0
    client.post(url, json={"uuids": uuids, "clear": "all"})
    assert client.get(f"/api/libraries/{library['id']}/findings").json()["gaps"][0]["fix"] is None


def test_places_come_from_immich(client, immich_server):
    assert client.get("/api/places", params={"name": "Lis"}).status_code == 409
    store, url = immich_server
    client.post("/api/connect", json={"url": url, "key": store.key})
    assert client.get("/api/places", params={"name": "L"}).status_code == 422
    places = client.get("/api/places", params={"name": "Lis"}).json()
    assert [p["name"] for p in places] == ["Lisbon", "Lisbon Falls"]
    assert places[0]["region"] == "Lisboa" and places[1]["region"] == "Maine"


def test_stop_ends_a_check(client, monkeypatch):
    library = added(client)
    runtime = client.app.state.runtime
    started = threading.Event()

    def slow(path, stop=None):
        started.set()
        while not stop.is_set():
            time.sleep(0.01)
        return None

    monkeypatch.setattr(check_module, "sha1_of", slow)
    lib = runtime.libraries[library["id"]]
    # Forget the checksums, so the next check reads the files again.
    with lib.state.lock, lib.state.db:
        lib.state.db.execute("DELETE FROM files")
    client.post(f"/api/libraries/{library['id']}/check", json={})
    assert started.wait(5)
    client.post("/api/stop", json={})
    library = settle(client)
    assert library["scan"]["check"]["complete"] is False


def test_the_check_is_kept_after_a_restart(client):
    library = added(client)
    from immigrate.server import Runtime

    restarted = Runtime(client.app.state.runtime.config)
    try:
        assert restarted.libraries[library["id"]].check is not None
    finally:
        restarted.close()


def test_the_plan_counts_what_is_found_in_immich(client, immich_server):
    store, url = immich_server
    client.post("/api/connect", json={"url": url, "key": store.key})
    library = added(client)
    preview = client.post(f"/api/libraries/{library['id']}/preview", json={}).json()
    assert preview["linked"] == 0 and preview["new"] == 2


def test_dates_are_not_parsed_from_the_fix_in_utc_by_mistake():
    moment = datetime(2019, 7, 1, 10, tzinfo=UTC)
    assert moment.isoformat() == "2019-07-01T10:00:00+00:00"
