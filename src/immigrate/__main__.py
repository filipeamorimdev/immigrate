"""Console entry point: ``immigrate`` or ``python -m immigrate``."""

from __future__ import annotations

import logging

import uvicorn

from .config import Config
from .timezones import Formatter


def main() -> None:
    config = Config.from_env()
    handler = logging.StreamHandler()
    handler.setFormatter(Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s"))
    logging.basicConfig(level=config.log_level, handlers=[handler])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        "immigrate.server:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        access_log=False,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
