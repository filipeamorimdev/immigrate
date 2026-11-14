"""Small Immich API client, written against the v3.3.5 OpenAPI spec."""

from __future__ import annotations

import mimetypes
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

import httpx

mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("video/quicktime", ".mov")

CHECK_CHUNK = 1000
UPDATE_CHUNK = 500


class ImmichError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Upload:
    id: str
    duplicate: bool
    sent: bool = True  # False when Immigrate linked to an asset without uploading


class Reader:
    """A file being uploaded, which reports how far the upload has read into it."""

    def __init__(self, handle: BinaryIO, progress: Callable[[int], None]) -> None:
        self.handle, self.progress = handle, progress

    def read(self, size: int = -1) -> bytes:
        data = self.handle.read(size)
        self.progress(self.handle.tell())
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.handle.seek(offset, whence)

    def tell(self) -> int:
        return self.handle.tell()

    def fileno(self) -> int:
        return self.handle.fileno()

    def close(self) -> None:
        self.handle.close()


class Immich:
    def __init__(
        self,
        url: str,
        api_key: str,
        retries: int = 4,
        backoff: float = 1.5,
        connect_timeout: float = 60.0,
    ):
        self.url = url.rstrip("/")
        if self.url.endswith("/api"):
            self.url = self.url[:-4]
        self.retries = retries
        self.backoff = backoff
        self.client = httpx.Client(
            base_url=f"{self.url}/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(connect_timeout, read=None, write=None),
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def _request(
        self,
        method: str,
        path: str,
        make_kwargs: Callable[[], dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                request_kwargs = make_kwargs() if make_kwargs else kwargs
                response = self.client.request(method, path, **request_kwargs)
            except httpx.TransportError as exc:
                last = ImmichError(f"Couldn't reach Immich: {exc}")
            else:
                if response.status_code < 400:
                    return response
                detail = _detail(response)
                error = ImmichError(f"{method} {path}: {detail}", response.status_code)
                if response.status_code != 429 and response.status_code < 500:
                    raise error
                last = error
            if attempt < self.retries:
                time.sleep(self.backoff * attempt)
        raise last or ImmichError("No attempts were made.")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        return response.json() if response.content else None

    def version(self) -> dict[str, Any]:
        """The server's version. Immich answers this without an API key."""
        version = self._json("GET", "/server/version")
        return {
            "version": f"{version['major']}.{version['minor']}.{version['patch']}",
            "major": version["major"],
        }

    def identity(self) -> dict[str, Any]:
        user = self._json("GET", "/users/me")
        return {"name": user.get("name") or user.get("email"), "email": user.get("email")} | (
            self.version()
        )

    def upload(
        self,
        path: str,
        filename: str,
        created: str,
        modified: str,
        *,
        favorite: bool = False,
        visibility: str = "timeline",
        live_video_id: str | None = None,
        sidecar: bytes | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> Upload:
        """Upload one file. `progress` hears how many bytes of it have been read so far."""
        # No `filename` field: Immich checks every file part against it, so the
        # sidecar would be judged as IMG_0001.JPG and rejected. The asset part's
        # own name becomes originalFileName instead.
        fields = {
            "fileCreatedAt": created,
            "fileModifiedAt": modified,
            "isFavorite": "true" if favorite else "false",
            "visibility": visibility,
        }
        if live_video_id:
            fields["livePhotoVideoId"] = live_video_id
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        handles: list[BinaryIO] = []

        def attempt() -> dict[str, Any]:
            # Reopened on every retry so a failed upload restarts from byte zero.
            handles.append(open(path, "rb"))
            data: Any = Reader(handles[-1], progress) if progress else handles[-1]
            files: dict[str, tuple[str, Any, str]] = {"assetData": (filename, data, mime)}
            if sidecar:
                files["sidecarData"] = (f"{filename}.xmp", sidecar, "application/xml")
            return {"data": fields, "files": files}

        try:
            response = self._request("POST", "/assets", make_kwargs=attempt)
        finally:
            for handle in handles:
                handle.close()
        body = response.json()
        return Upload(body["id"], body["status"] == "duplicate")

    def update_asset(self, asset_id: str, **fields: Any) -> None:
        self._json("PUT", f"/assets/{asset_id}", json=fields)

    def create_stack(self, asset_ids: list[str]) -> None:
        self._json("POST", "/stacks", json={"assetIds": asset_ids})

    def albums(self) -> dict[str, dict[str, Any]]:
        return {album["albumName"]: album for album in self._json("GET", "/albums")}

    def create_album(self, name: str) -> str:
        return str(self._json("POST", "/albums", json={"albumName": name})["id"])

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        results = self._json("PUT", f"/albums/{album_id}/assets", json={"ids": asset_ids})
        rejected = [
            r for r in results or [] if not r.get("success") and r.get("error") != "duplicate"
        ]
        if rejected:
            raise ImmichError(
                f"Immich rejected {len(rejected)} album items: {rejected[0].get('error')}"
            )

    def search_ids(self, **filters: Any) -> Iterator[str]:
        page: int | None = 1
        while page:
            body = self._json(
                "POST", "/search/metadata", json={"page": page, "size": 1000, **filters}
            )
            assets = body["assets"]
            for asset in assets["items"]:
                yield asset["id"]
            page = int(assets["nextPage"]) if assets.get("nextPage") else None

    def album_asset_ids(self, album_id: str) -> set[str]:
        # Album responses stopped including assets in Immich v3.
        return set(self.search_ids(albumIds=[album_id]))

    def existing(self, checksums: list[str]) -> dict[str, tuple[str, bool]]:
        """Return the asset ID, and whether it's in the trash, for each checksum Immich has."""
        found: dict[str, tuple[str, bool]] = {}
        for start in range(0, len(checksums), CHECK_CHUNK):
            chunk = checksums[start : start + CHECK_CHUNK]
            body = self._json(
                "POST",
                "/assets/bulk-upload-check",
                json={"assets": [{"id": checksum, "checksum": checksum} for checksum in chunk]},
            )
            for result in body["results"]:
                if result.get("action") == "reject" and result.get("assetId"):
                    found[result["id"]] = (result["assetId"], bool(result.get("isTrashed")))
        return found

    def update_assets(self, asset_ids: list[str], **fields: Any) -> None:
        for start in range(0, len(asset_ids), UPDATE_CHUNK):
            chunk = asset_ids[start : start + UPDATE_CHUNK]
            self._request("PUT", "/assets", json={"ids": chunk, **fields})

    def places(self, name: str) -> list[dict[str, Any]]:
        results = self._json("GET", "/search/places", params={"name": name})
        return [
            {
                "name": place["name"],
                "region": ", ".join(
                    dict.fromkeys(
                        part
                        for part in (place.get("admin2name"), place.get("admin1name"))
                        if part and part != place["name"]
                    )
                ),
                "latitude": place["latitude"],
                "longitude": place["longitude"],
            }
            for place in results or []
        ]


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        message = None
    return f"HTTP {response.status_code} {message or response.text[:200]}"
