# Architecture

Immigrate is one Python process that serves a static page and syncs one or more
libraries. A library is either an Apple Photos library or a plain folder. It runs at
most one scan and one sync at a time, across all libraries. It's single-user by design.

```text
Browser ──HTTP──> server.py ──> library.py ──> osxphotos ──> /nas (read-only)
                     │
                     ├───────> folder_library.py ──────────> /nas (read-only)
                     │
                     ├───────> folders.py  (lists folders to find the library)
                     │
                     ├───────> libraries.py (one per library: state, scan, choices)
                     │       └──> check.py ──> metadata.py ──> exiftool
                     │                └──────> immich.py (what Immich already has)
                     │
                     └───────> sync.py ──> immich.py ──HTTP──> Immich
                                  │
                                  ├──> xmp.py     (sidecar for each item)
                                  └──> state.py   (SQLite in /data/libraries/<id>)
```

## Modules

| Module | Responsibility |
| --- | --- |
| `config.py` | Reads environment variables |
| `timezones.py` | The time zone chosen in Settings, used for the log and the page |
| `folders.py` | Lists folders under the shared NAS folder, and decides which can be chosen |
| `library.py` | Turns the Photos database into `Item` and `Part` records |
| `folder_library.py` | Turns a plain folder into the same `Item` and `Part` records |
| `check.py` | After a read: checksums, copies, what Immich has, and missing dates and locations |
| `metadata.py` | Reads dates and locations from files with exiftool |
| `findings.py` | Shapes what the check found for the page, with location suggestions |
| `xmp.py` | Builds the sidecar that carries dates, GPS, captions, and tags |
| `immich.py` | Immich API client, with retries for 429, 5xx, and network errors |
| `state.py` | Remembers what was sent, so runs can resume and stay incremental |
| `libraries.py` | One library's records, saved scan, choices, and schedule |
| `sync.py` | Plans, runs, and verifies a sync |
| `server.py` | HTTP API, the list of libraries, and the scheduled sync loop |
| `static/` | Setup steps, libraries, and settings, in plain HTML, CSS, and JavaScript |

## How a read and check work

1. The read turns the library into a `Scan` and saves it as `scan.json.gz`.
2. The check stats every file, and reads only those whose size or modification time
   changed since the last check. It saves each file's SHA-1, and for a folder its date
   and location, in the `files` table of `state.db`.
3. Identical originals become copies of one kept item. The one with a sidecar, a
   location, or albums is kept.
4. When Immich is connected, `POST /assets/bulk-upload-check` says which checksums it
   already has. The result is saved as `check.json.gz`, which belongs to one read.

## How a sync works

1. `plan()` compares each item's fingerprint with `state.py`. It doesn't contact Immich.
2. `Job.run_items()` uploads changed items in a thread pool. Each item goes in this order:
   Live Photo video, original, edit, RAW. The parts are then stacked with the edit first.
   A file whose checksum Immich already has is linked instead of uploaded, unless it
   changed since the check. Copies go after the items they copy.
3. `Job.run_fixes()` sets the dates and locations the user chose with `PUT /assets`,
   one call for each set of values. What was applied is recorded, so it's sent once.
4. `Job.run_albums()` adds any missing assets to each album. It never removes any.
5. `verify()` lists what Immich holds and compares that with the library.

## Decisions worth keeping

- **Direct upload rather than export.** Nothing is copied to temporary storage,
  which matters on a nearly full pool.
- **Immich's checksum handles duplicates.** If state is lost, or a file arrived
  through the phone app, Immich returns `duplicate` with the existing asset ID,
  so the run reconnects to it instead of creating a copy.
- **Album contents come from search.** Immich v3 removed `assets` from album
  responses, so `Immich.album_asset_ids()` uses `POST /search/metadata`.
- **Metadata goes in a sidecar.** Apple keeps edits to dates and locations in its
  database, not in the file, so the sidecar carries them and the original stays
  byte-identical.
- **Each library has its own folder, `/data/libraries/<id>`.** It holds `state.db`
  and the saved scan, `scan.json.gz`. The ID comes from the library's path, so a
  library that's removed and added again finds its old records. Removing a library
  only takes it off the list. Earlier versions kept one library in
  `/data/photobridge.db`. That's moved into its folder on the first start.
- **The saved scan is loaded again after a restart or rebuild.** Scheduled syncs and
  Start sync always read the library again first, so a saved scan never goes stale there.
- **Choices belong to each library.** Settings holds the defaults a new library starts
  with. A new library isn't put on a schedule until its first sync is started by hand.
- **The schedule skips libraries that haven't changed.** It compares the modification
  time of `Photos.sqlite` with the one from the last sync that finished with no failures.
  A folder has no database, so it uses a hash of each file's path, size, and modification
  time instead. That means listing the whole folder at every check.
- **A library's kind is saved, not worked out from its path.** Older versions could point
  at a Photos library whose path doesn't end in `.photoslibrary`, and treating one as a
  folder would upload its thumbnails. A saved library with no kind is a Photos library.
- **Immigrate builds no sidecar for a folder's files.** Immich prefers a sidecar's dates
  to the file's own, and a folder only knows when a file last changed, so a built sidecar
  would replace real capture dates. Immich reads EXIF, then falls back to the file time
  sent with the upload. A sidecar already beside a file is sent as it is.
- **Live Photos in a folder aren't paired by name.** The video of a pair is hidden in
  Immich, so a wrong match would make a real video disappear. Immich links the two itself
  when both files carry the same content ID, as Apple's exports do.
- **A folder's albums are off until asked for.** A dated folder tree could make hundreds
  of albums, and Immigrate never deletes anything from Immich.
- **Folder scans skip system folders and photo apps' libraries.** That covers hidden
  folders, anything starting with `@`, NAS trash and snapshots, and `.photoslibrary`,
  `.photolibrary`, and `.aplibrary` bundles, which are full of thumbnails. Symlinks aren't
  followed, so a scan can't leave the shared folder.
- **The check runs after every read.** Start sync and the schedule read, check, then
  sync, so a sync always knows what Immich has. Only changed files are read again.
- **Dates and locations the user sets live in `state.db`, never in the library.** They're
  applied with `PUT /assets`, which also works on assets Immigrate didn't upload. A date
  is sent with its time zone offset, so Immich keeps that time zone, and Immich locks
  the value so its own metadata extraction doesn't replace it.
- **The page doesn't show photos.** It has no login, and browsers can't show HEIC or
  RAW. Items already in Immich link to it, behind Immich's own login.
