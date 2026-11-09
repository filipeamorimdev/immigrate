"""Turn a plain folder of photos and videos into the records a Photos library gives.

Only names, sizes, and times are read here. Nothing in the folder is changed.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import timezones
from .folders import is_app_library, is_system_folder
from .library import Item, Part, Scan

# From server/src/utils/mime-types.ts in Immich v3.3.5. Immich counts .psd as RAW,
# but a Photoshop file beside a JPEG isn't that JPEG's camera original.
RAW_EXTENSIONS = frozenset(
    [
        ".3fr",
        ".ari",
        ".arw",
        ".cap",
        ".cin",
        ".cr2",
        ".cr3",
        ".crw",
        ".dcr",
        ".dng",
        ".erf",
        ".fff",
        ".iiq",
        ".k25",
        ".kdc",
        ".mrw",
        ".nef",
        ".nrw",
        ".orf",
        ".ori",
        ".pef",
        ".raf",
        ".raw",
        ".rw2",
        ".rwl",
        ".sr2",
        ".srf",
        ".srw",
        ".x3f",
    ]
)
IMAGE_EXTENSIONS = frozenset(
    [
        ".avif",
        ".bmp",
        ".gif",
        ".heic",
        ".heif",
        ".hif",
        ".insp",
        ".jfif",
        ".jp2",
        ".jpe",
        ".jpeg",
        ".jpg",
        ".jxl",
        ".mpo",
        ".png",
        ".psd",
        ".svg",
        ".tif",
        ".tiff",
        ".webp",
    ]
)
VIDEO_EXTENSIONS = frozenset(
    [
        ".3gp",
        ".3gpp",
        ".avi",
        ".flv",
        ".insv",
        ".m2t",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpe",
        ".mpeg",
        ".mpg",
        ".mts",
        ".mxf",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    ]
)
ASSET_EXTENSIONS = RAW_EXTENSIONS | IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
SIDECAR_EXTENSION = ".xmp"


@dataclass
class _File:
    path: str
    name: str
    size: int
    mtime_ns: int

    @property
    def extension(self) -> str:
        return os.path.splitext(self.name)[1].casefold()


def clean_name(name: str) -> str:
    """Make a name safe to send. Older NAS folders can hold names that aren't valid UTF-8."""
    return name.encode("utf-8", "replace").decode()


def _list_folder(folder: str) -> tuple[list[_File], dict[str, str], list[str]]:
    files: list[_File] = []
    sidecars: dict[str, str] = {}
    subfolders: list[str] = []
    with os.scandir(folder) as listing:
        entries = sorted(listing, key=lambda entry: entry.name)
    for entry in entries:
        name = entry.name
        try:
            if entry.is_symlink():
                # Following one could leave the shared folder or read the same files twice.
                continue
            if entry.is_dir():
                if not is_system_folder(name) and not is_app_library(name):
                    subfolders.append(entry.path)
                continue
            if name.startswith(".") or not entry.is_file():
                continue
            extension = os.path.splitext(name)[1].casefold()
            if extension == SIDECAR_EXTENSION:
                sidecars[name.casefold()] = entry.path
            elif extension in ASSET_EXTENSIONS:
                stat = entry.stat()
                files.append(_File(entry.path, name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            # It vanished or can't be examined, so it can't be sent either.
            continue
    return files, sidecars, subfolders


def _walk(root: str, unreadable: list[str]) -> Iterator[tuple[str, list[_File], dict[str, str]]]:
    """Go through the folders in a fixed order. Only a problem with `root` itself is raised."""
    waiting = [root]
    while waiting:
        folder = waiting.pop()
        if os.path.exists(os.path.join(folder, "database", "Photos.sqlite")):
            # A Photos library that lost its suffix. Its files include thousands of thumbnails.
            continue
        try:
            files, sidecars, subfolders = _list_folder(folder)
        except OSError:
            if folder == root:
                raise
            unreadable.append(clean_name(os.path.relpath(folder, root)))
            continue
        waiting.extend(reversed(subfolders))
        yield folder, files, sidecars


def _find_sidecar(sidecars: dict[str, str], stem: str, files: list[_File]) -> str:
    names = [f"{file.name}{SIDECAR_EXTENSION}" for file in files]
    for name in [*names, f"{stem}{SIDECAR_EXTENSION}"]:
        if found := sidecars.get(name.casefold()):
            return found
    return ""


def _to_item(
    root: str,
    album: str,
    main: _File,
    raw: _File | None,
    sidecars: dict[str, str],
    stem: str,
) -> Item:
    # Always UTC, so choosing another time zone in Settings doesn't make every file look changed.
    changed = datetime.fromtimestamp(main.mtime_ns / 1e9, UTC)
    filename = clean_name(main.name)
    item = Item(
        uuid=hashlib.sha1(
            os.fsencode(os.path.relpath(main.path, root)), usedforsecurity=False
        ).hexdigest(),
        filename=filename,
        taken=changed,
        modified=changed,
        is_video=main.extension in VIDEO_EXTENSIONS,
        from_folder=True,
        sidecar_path=_find_sidecar(sidecars, stem, [main] if raw is None else [main, raw]),
        albums=[album],
        parts=[Part("main", main.path, filename, main.size)],
    )
    if raw is not None:
        item.parts.append(Part("raw", raw.path, clean_name(raw.name), raw.size))
    return item


def _items_in_folder(
    root: str, album: str, files: list[_File], sidecars: dict[str, str]
) -> Iterator[Item]:
    by_stem: dict[str, list[_File]] = {}
    for file in files:
        by_stem.setdefault(os.path.splitext(file.name)[0].casefold(), []).append(file)
    for stem, group in by_stem.items():
        raws = [file for file in group if file.extension in RAW_EXTENSIONS]
        photos = [file for file in group if file.extension in IMAGE_EXTENSIONS]
        # Only a clear match is a pair. Anything less clear stays as separate items.
        paired = len(raws) == 1 and len(photos) == 1
        for file in group:
            if paired and file is raws[0]:
                continue
            raw = raws[0] if paired and file is photos[0] else None
            yield _to_item(root, album, file, raw, sidecars, stem)


def scan_folder(path: str, progress: Callable[[int, int], None] | None = None) -> Scan:
    title = clean_name(Path(path).name) or "NAS"
    unreadable: list[str] = []
    items: list[Item] = []
    for folder, files, sidecars in _walk(path, unreadable):
        relative = clean_name(Path(folder).relative_to(path).as_posix())
        album = title if relative == "." else f"{title}/{relative}"
        items.extend(_items_in_folder(path, album, files, sidecars))
        if progress:
            # There's no total until the last folder has been opened.
            progress(len(items), 0)
    if progress:
        progress(len(items), len(items))
    items.sort(key=lambda i: (i.taken.timestamp(), i.uuid))
    return Scan(
        library=path,
        photos_version="",
        items=items,
        album_names=sorted({name for item in items for name in item.albums}),
        people_count=0,
        scanned_at=timezones.now().isoformat(timespec="seconds"),
        unreadable=sorted(unreadable),
    )


def folder_marker(path: str) -> str:
    """Summarize the folder, so the schedule can tell when something in it changed."""
    digest = hashlib.sha1(usedforsecurity=False)
    for _, files, _ in _walk(path, []):
        for file in files:
            digest.update(os.fsencode(f"{file.path}\0{file.size}\0{file.mtime_ns}\0"))
    return digest.hexdigest()
