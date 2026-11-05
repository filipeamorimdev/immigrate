# Architecture

Immigrate is one Python process that serves a static page and syncs one or more
libraries. It runs at most one scan and one sync at a time, across all libraries.
It's single-user by design.

```text
Browser ──HTTP──> server.py ──> library.py ──> osxphotos ──> /nas (read-only)
                     │
                     ├───────> folders.py  (lists folders to find the library)
                     │
                     ├───────> libraries.py (one per library: state, scan, choices)
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
| `folders.py` | Lists folders under the shared NAS folder, so the page can find the library |
| `library.py` | Turns the Photos database into `Item` and `Part` records |
| `xmp.py` | Builds the sidecar that carries dates, GPS, captions, and tags |
| `immich.py` | Immich API client, with retries for 429, 5xx, and network errors |
| `state.py` | Remembers what was sent, so runs can resume and stay incremental |
| `libraries.py` | One library's records, saved scan, choices, and schedule |
| `sync.py` | Plans, runs, and verifies a sync |
| `server.py` | HTTP API, the list of libraries, and the scheduled sync loop |
| `static/` | Setup steps, libraries, and settings, in plain HTML, CSS, and JavaScript |

## How a sync works

1. `plan()` compares each item's fingerprint with `state.py`. It doesn't contact Immich.
2. `Job.run_items()` uploads changed items in a thread pool. Each item goes in this order:
   Live Photo video, original, edit, RAW. The parts are then stacked with the edit first.
3. `Job.run_albums()` adds any missing assets to each album. It never removes any.
4. `verify()` lists what Immich holds and compares that with the library.

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
