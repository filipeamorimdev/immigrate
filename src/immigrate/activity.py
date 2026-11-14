"""The recent log lines the page shows, kept in memory."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

KEPT = 1000


class Activity:
    """The last lines of what Immigrate did. Each has a number, so the page asks for new ones."""

    def __init__(self, kept: int = KEPT) -> None:
        self.lines: deque[dict[str, Any]] = deque(maxlen=kept)
        self.last = 0
        self.lock = threading.Lock()

    def add(self, text: str, level: str = "info", at: float | None = None) -> None:
        with self.lock:
            self.last += 1
            self.lines.append(
                {"n": self.last, "at": at or time.time(), "level": level, "text": text}
            )

    def since(self, after: int) -> dict[str, Any]:
        with self.lock:
            return {"last": self.last, "lines": [line for line in self.lines if line["n"] > after]}


class ActivityHandler(logging.Handler):
    """Copies what Immigrate logs into the lines the page shows, without tracebacks."""

    def __init__(self, activity: Activity) -> None:
        super().__init__(logging.INFO)
        self.activity = activity

    def emit(self, record: logging.LogRecord) -> None:
        level = "error" if record.levelno >= logging.ERROR else "info"
        if record.levelno == logging.WARNING:
            level = "warning"
        self.activity.add(record.getMessage(), level, record.created)
