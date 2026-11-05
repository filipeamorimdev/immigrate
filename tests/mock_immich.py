"""An in-memory Immich that enforces the v3.3.5 OpenAPI contract for the calls Immigrate makes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
VISIBILITY = {"archive", "timeline", "hidden", "locked"}
# Extensions Immich accepts as assets (server/src/utils/mime-types.ts).
ASSET_EXTENSIONS = {
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
    ".psd",
    ".raf",
    ".raw",
    ".rw2",
    ".rwl",
    ".sr2",
    ".srf",
    ".srw",
    ".x3f",
    ".avif",
    ".bmp",
    ".gif",
    ".jfif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
    ".heic",
    ".heif",
    ".hif",
    ".insp",
    ".jp2",
    ".jpe",
    ".jxl",
    ".mpo",
    ".svg",
    ".tif",
    ".tiff",
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
}
UPLOAD_FIELDS = {
    "assetData",
    "sidecarData",
    "fileCreatedAt",
    "fileModifiedAt",
    "filename",
    "isFavorite",
    "visibility",
    "livePhotoVideoId",
    "duration",
    "metadata",
}


class Store:
    def __init__(self):
        self.assets, self.albums, self.stacks = {}, {}, []
        self.by_checksum = {}
        self.calls, self.violations = [], []
        self.fail_uploads = 0  # next N uploads return 500
        self.fail_filenames = set()  # these always return 500
        self.lock = threading.Lock()
        self.key = "test-key"


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def reply(self, status, body=None):
            data = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def bad(self, why):
            store.violations.append(f"{self.command} {self.path}: {why}")
            self.reply(400, {"message": why})

        def handle_any(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            path = self.path.split("?")[0]
            store.calls.append((self.command, path))
            if path == "/api/server/version":
                return self.reply(200, {"major": 3, "minor": 3, "patch": 5})
            if self.headers.get("x-api-key") != store.key:
                return self.reply(401, {"message": "Invalid API key"})
            if path == "/api/users/me":
                return self.reply(200, {"id": "u", "name": "Test", "email": "t@example.com"})
            if path == "/api/assets" and self.command == "POST":
                return self.upload(raw)
            if path == "/api/stacks" and self.command == "POST":
                ids = json.loads(raw).get("assetIds")
                if not ids or len(ids) < 2 or any(i not in store.assets for i in ids):
                    return self.bad("stack needs 2+ known ids")
                store.stacks.append(ids)
                return self.reply(
                    201, {"id": str(uuid.uuid4()), "primaryAssetId": ids[0], "assets": []}
                )
            if path == "/api/albums" and self.command == "GET":
                return self.reply(200, [self.album_dto(a) for a in store.albums.values()])
            if path == "/api/albums" and self.command == "POST":
                name = json.loads(raw).get("albumName")
                if not name:
                    return self.bad("albumName required")
                album = {"id": str(uuid.uuid4()), "albumName": name, "assets": []}
                store.albums[album["id"]] = album
                return self.reply(201, self.album_dto(album))
            if match := re.fullmatch(r"/api/albums/([^/]+)/assets", path):
                album = store.albums.get(match[1])
                if not album or self.command != "PUT":
                    return self.reply(404, {"message": "not found"})
                results = []
                for asset_id in json.loads(raw)["ids"]:
                    if asset_id in album["assets"]:
                        results.append({"id": asset_id, "success": False, "error": "duplicate"})
                    elif asset_id not in store.assets:
                        results.append({"id": asset_id, "success": False, "error": "not_found"})
                    else:
                        album["assets"].append(asset_id)
                        results.append({"id": asset_id, "success": True})
                return self.reply(200, results)
            if path == "/api/search/metadata":
                return self.search(json.loads(raw))
            if match := re.fullmatch(r"/api/assets/([^/]+)", path):
                asset = store.assets.get(match[1])
                if not asset or self.command != "PUT":
                    return self.reply(404, {"message": "not found"})
                body = json.loads(raw)
                if set(body) - {
                    "isFavorite",
                    "livePhotoVideoId",
                    "visibility",
                    "description",
                    "rating",
                    "dateTimeOriginal",
                    "latitude",
                    "longitude",
                }:
                    return self.bad(f"unknown fields {body}")
                asset.update(body)
                return self.reply(200, {k: v for k, v in asset.items() if k != "sidecar"})
            self.reply(404, {"message": f"no route {path}"})

        def album_dto(self, album):
            # v3 removed `assets` from album responses.
            return {
                "id": album["id"],
                "albumName": album["albumName"],
                "assetCount": len(album["assets"]),
            }

        def upload(self, raw):
            content_type = self.headers.get("content-type", "")
            if not content_type.startswith("multipart/form-data"):
                return self.bad("must be multipart")
            message = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw
            )
            fields, files = {}, {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    files[name] = (
                        part.get_filename(),
                        part.get_payload(decode=True),
                        part.get_content_type(),
                    )
                else:
                    fields[name] = part.get_payload(decode=True).decode()
            unknown = (set(fields) | set(files)) - UPLOAD_FIELDS
            if unknown:
                return self.bad(f"unknown fields {unknown}")
            for required in ("fileCreatedAt", "fileModifiedAt"):
                if required not in fields or not re.match(
                    r"^\d{4}-\d\d-\d\dT\d\d:\d\d", fields[required]
                ):
                    return self.bad(f"{required} must be date-time: {fields.get(required)}")
            if "assetData" not in files:
                return self.bad("assetData required")
            if fields.get("visibility", "timeline") not in VISIBILITY:
                return self.bad("bad visibility")
            if fields.get("isFavorite") not in (None, "true", "false"):
                return self.bad("bad isFavorite")
            # Immich judges each file part by the `filename` field when it is set,
            # falling back to the part's own name.
            for field, (part_name, _, _) in files.items():
                name = fields.get("filename") or part_name
                extension = os.path.splitext(name)[1].lower()
                allowed = {".xmp"} if field == "sidecarData" else ASSET_EXTENSIONS
                if extension not in allowed:
                    return self.bad(f"Unsupported file type {name}")
            live = fields.get("livePhotoVideoId")
            if live and (not UUID.match(live) or live not in store.assets):
                return self.bad("livePhotoVideoId must be a known uuid")
            filename = fields.get("filename") or files["assetData"][0]
            with store.lock:
                if filename in store.fail_filenames or store.fail_uploads > 0:
                    if store.fail_uploads > 0:
                        store.fail_uploads -= 1
                    return self.reply(500, {"message": "boom"})
                checksum = hashlib.sha1(files["assetData"][1], usedforsecurity=False).hexdigest()
                if checksum in store.by_checksum:
                    return self.reply(
                        200, {"id": store.by_checksum[checksum], "status": "duplicate"}
                    )
                asset_id = str(uuid.uuid4())
                store.by_checksum[checksum] = asset_id
                store.assets[asset_id] = {
                    "id": asset_id,
                    "originalFileName": filename,
                    "fileCreatedAt": fields["fileCreatedAt"],
                    "isFavorite": fields.get("isFavorite") == "true",
                    "visibility": fields.get("visibility", "timeline"),
                    "livePhotoVideoId": live,
                    "sidecar": files.get("sidecarData", (None, None, None))[1],
                    "mime": files["assetData"][2],
                }
            self.reply(201, {"id": asset_id, "status": "created"})

        def search(self, body):
            size, page = body.get("size", 250), body.get("page", 1)
            if not isinstance(page, int) or not isinstance(size, int) or size > 1000:
                return self.bad("page and size must be integers, size <= 1000")
            items = list(store.assets.values())
            if ids := body.get("albumIds"):
                wanted = set(store.albums[ids[0]]["assets"])
                items = [a for a in items if a["id"] in wanted]
            visibility = body.get("visibility", "timeline")
            items = [a for a in items if a["visibility"] == visibility]
            chunk = items[(page - 1) * size : page * size]
            more = page * size < len(items)
            self.reply(
                200,
                {
                    "assets": {
                        "items": [{"id": a["id"]} for a in chunk],
                        "total": len(chunk),
                        "count": len(chunk),
                        "nextPage": str(page + 1) if more else None,
                    },
                    "albums": {"items": [], "total": 0, "count": 0},
                },
            )

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

    return Handler


def serve(store: Store, port: int = 0):
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
