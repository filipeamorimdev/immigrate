from __future__ import annotations

import os
from pathlib import Path

import pytest

from immigrate.library import Scan, scan_library

ROOT = Path(__file__).resolve().parent.parent
LIBRARIES = Path(os.environ.get("SAMPLE_LIBRARIES", ROOT / ".samples" / "tests"))


def _library(name: str) -> str:
    path = LIBRARIES / name
    if not path.exists():
        pytest.skip(f"{name} is missing. Run scripts/fetch-sample-libraries.sh first.")
    return str(path)


@pytest.fixture(scope="session")
def main_library() -> str:
    return _library("Test-26.1.photoslibrary")


@pytest.fixture(scope="session")
def scan(main_library: str) -> Scan:
    return scan_library(main_library)


@pytest.fixture(scope="session")
def live_scan() -> Scan:
    return scan_library(_library("Test-Live-15.7.2.photoslibrary"))
