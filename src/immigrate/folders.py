"""Browse the shared NAS folder so the page can find a Photos library or a folder of photos.

Only folder names are listed, and nothing outside the shared folder can be reached.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

LIBRARY_SUFFIX = ".photoslibrary"
MAX_FOLDERS = 500
PHOTOS = "photos"
FOLDER = "folder"
# These belong to a photo app, and much of what's inside them is thumbnails.
APP_LIBRARY_SUFFIXES = (LIBRARY_SUFFIX, ".photolibrary", ".aplibrary")
# Where NAS systems and operating systems keep trash, snapshots, and apps. Folders
# starting with @, such as Synology's @eaDir thumbnails, are treated the same way.
SYSTEM_FOLDERS = frozenset(
    {
        "#recycle",
        "#snapshot",
        "$recycle.bin",
        "ix-applications",
        "ix-apps",
        "lost+found",
        "system volume information",
    }
)


class FolderError(ValueError):
    pass


def is_system_folder(name: str) -> bool:
    return name.startswith((".", "@")) or name.casefold() in SYSTEM_FOLDERS


def is_app_library(name: str) -> bool:
    return name.casefold().endswith(APP_LIBRARY_SUFFIXES)


def _relative(base: Path, path: Path) -> str:
    return "" if path == base else path.relative_to(base).as_posix()


def resolve(root: str, relative: str) -> Path:
    base = Path(root).resolve()
    target = (base / relative.strip("/")).resolve()
    if target != base and base not in target.parents:
        raise FolderError("That folder is outside the shared NAS folder.")
    return target


def display_path(root: str, path: str) -> str:
    """Show a path the way the page does, relative to the shared folder when it's inside it."""
    base = Path(root).resolve()
    target = Path(path).resolve()
    return _relative(base, target) if target == base or base in target.parents else path


def list_folders(root: str, relative: str = "") -> dict[str, Any]:
    base = Path(root).resolve()
    target = resolve(root, relative)
    try:
        with os.scandir(target) as entries:
            names = sorted(
                (e.name for e in entries if not is_system_folder(e.name) and e.is_dir()),
                key=str.casefold,
            )
    except OSError as exc:
        raise FolderError("Immigrate can't open this folder.") from exc
    path = _relative(base, target)
    return {
        "path": path,
        "parent": None if target == base else _relative(base, target.parent),
        "folders": [
            {
                "name": name,
                "path": f"{path}/{name}" if path else name,
                "library": name.endswith(LIBRARY_SUFFIX),
            }
            for name in names[:MAX_FOLDERS]
        ],
        "more": max(0, len(names) - MAX_FOLDERS),
    }


def library_at(root: str, relative: str) -> Path:
    target = resolve(root, relative)
    if not target.name.endswith(LIBRARY_SUFFIX):
        raise FolderError("Choose a folder whose name ends in .photoslibrary.")
    if not (target / "database" / "Photos.sqlite").is_file():
        raise FolderError("That library has no Photos database. Check that it copied completely.")
    return target


def folder_at(root: str, relative: str) -> Path:
    target = resolve(root, relative)
    names = target.relative_to(Path(root).resolve()).parts
    if any(is_system_folder(name) or is_app_library(name) for name in names):
        raise FolderError("Immigrate doesn't read that folder. Choose another one.")
    if not target.is_dir():
        raise FolderError("Immigrate can't open this folder.")
    return target


def source_at(root: str, relative: str) -> tuple[Path, str]:
    """Work out what the chosen path is: a Photos library, or any other folder."""
    if resolve(root, relative).name.endswith(LIBRARY_SUFFIX):
        return library_at(root, relative), PHOTOS
    return folder_at(root, relative), FOLDER
