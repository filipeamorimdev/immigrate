"""Resumable sync state, kept in SQLite under /data."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class State:
    def __init__(self, path: str | Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                uuid TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                primary_id TEXT,
                asset_ids TEXT NOT NULL DEFAULT '{}',
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS albums (title TEXT PRIMARY KEY, album_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def all_items(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            rows = self.db.execute(
                "SELECT uuid, fingerprint, status, primary_id, asset_ids, error FROM items"
            ).fetchall()
        return {
            row[0]: {
                "fingerprint": row[1],
                "status": row[2],
                "primary_id": row[3],
                "asset_ids": json.loads(row[4]),
                "error": row[5],
            }
            for row in rows
        }

    def counts(self) -> dict[str, int]:
        """Items per status, cheap enough to ask for on every page refresh."""
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall()
        return dict(rows)

    def save_item(
        self,
        uuid: str,
        fingerprint: str,
        status: str,
        primary_id: str | None,
        asset_ids: dict[str, str],
        error: str | None = None,
    ) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?)",
                (uuid, fingerprint, status, primary_id, json.dumps(asset_ids), error),
            )

    def album_id(self, title: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT album_id FROM albums WHERE title = ?", (title,)
            ).fetchone()
        return row[0] if row else None

    def save_album(self, title: str, album_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO albums VALUES (?, ?)", (title, album_id))

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))
