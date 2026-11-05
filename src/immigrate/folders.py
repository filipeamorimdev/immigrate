"""Browse the shared NAS folder so the page can find a Photos library.

Only folder names are listed, and nothing outside the shared folder can be reached.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

LIBRARY_SUFFIX = ".photoslibrary"
MAX_FOLDERS = 500


class FolderError(ValueError):
    pass


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
                (e.name for e in entries if not e.name.startswith(".") and e.is_dir()),
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
