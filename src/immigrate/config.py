"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    library_path: str | None
    host: str
    port: int
    log_level: str
    immich_url: str | None
    immich_api_key: str | None
    library_root: str = "/nas"

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            library_path=os.environ.get("LIBRARY_PATH") or None,
            host=os.environ.get("IMMIGRATE_HOST", "0.0.0.0"),
            port=int(os.environ.get("IMMIGRATE_PORT", "8088")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            immich_url=os.environ.get("IMMICH_URL") or None,
            immich_api_key=os.environ.get("IMMICH_API_KEY") or None,
            library_root=os.environ.get("LIBRARY_ROOT", "/nas"),
        )
