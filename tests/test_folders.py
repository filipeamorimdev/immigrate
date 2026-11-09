from __future__ import annotations

import pytest

from immigrate.folders import FolderError, display_path, library_at, list_folders, source_at


@pytest.fixture
def root(tmp_path):
    (tmp_path / "pool" / "shared").mkdir(parents=True)
    (tmp_path / "pool" / ".hidden").mkdir()
    (tmp_path / "pool" / "notes.txt").write_text("not a folder")
    database = tmp_path / "pool" / "shared" / "Photos.photoslibrary" / "database"
    database.mkdir(parents=True)
    (database / "Photos.sqlite").write_bytes(b"")
    (tmp_path / "pool" / "shared" / "Empty.photoslibrary").mkdir()
    return tmp_path


def test_lists_only_visible_folders(root):
    listing = list_folders(str(root))
    assert listing["path"] == "" and listing["parent"] is None
    assert [f["name"] for f in listing["folders"]] == ["pool"]
    pool = list_folders(str(root), "pool")
    assert pool["parent"] == ""
    assert [f["path"] for f in pool["folders"]] == ["pool/shared"]


def test_system_folders_are_not_listed(root):
    (root / "pool" / "@eaDir").mkdir()
    (root / "pool" / "#recycle").mkdir()
    assert [f["name"] for f in list_folders(str(root), "pool")["folders"]] == ["shared"]


def test_any_other_folder_can_be_chosen_and_a_library_stays_a_library(root):
    assert source_at(str(root), "pool/shared") == (root.resolve() / "pool/shared", "folder")
    assert source_at(str(root), "") == (root.resolve(), "folder")
    library = "pool/shared/Photos.photoslibrary"
    assert source_at(str(root), library) == (root.resolve() / library, "photos")
    with pytest.raises(FolderError, match="no Photos database"):
        source_at(str(root), "pool/shared/Empty.photoslibrary")
    with pytest.raises(FolderError, match="doesn't read"):
        source_at(str(root), "pool/.hidden")
    with pytest.raises(FolderError, match="outside"):
        source_at(str(root), "..")


def test_marks_photos_libraries(root):
    shared = list_folders(str(root), "pool/shared")
    assert {f["name"]: f["library"] for f in shared["folders"]} == {
        "Empty.photoslibrary": True,
        "Photos.photoslibrary": True,
    }


def test_cannot_leave_the_shared_folder(root, tmp_path_factory):
    with pytest.raises(FolderError):
        list_folders(str(root / "pool"), "..")
    outside = tmp_path_factory.mktemp("outside")
    (root / "pool" / "escape").symlink_to(outside)
    with pytest.raises(FolderError):
        list_folders(str(root / "pool"), "escape")


def test_missing_folder_gets_a_clear_message(root):
    with pytest.raises(FolderError, match="can't open"):
        list_folders(str(root), "nowhere")


def test_only_a_real_library_can_be_chosen(root):
    chosen = library_at(str(root), "pool/shared/Photos.photoslibrary")
    assert display_path(str(root), str(chosen)) == "pool/shared/Photos.photoslibrary"
    with pytest.raises(FolderError, match=r"ends in \.photoslibrary"):
        library_at(str(root), "pool/shared")
    with pytest.raises(FolderError, match="no Photos database"):
        library_at(str(root), "pool/shared/Empty.photoslibrary")


def test_paths_outside_the_shared_folder_are_shown_in_full(root):
    assert display_path(str(root / "pool"), "/library") == "/library"
