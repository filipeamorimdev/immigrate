# Changelog

This project follows [Semantic Versioning](https://semver.org).

## Unreleased

- Renamed from PhotoBridge to Immigrate. The image is now
  `ghcr.io/filipeamorimdev/immigrate`, and the command and Python package are
  `immigrate`. `PHOTOBRIDGE_UID`, `PHOTOBRIDGE_GID`, `PHOTOBRIDGE_PORT`, and
  `PHOTOBRIDGE_HOST` are now `IMMIGRATE_UID`, `IMMIGRATE_GID`, `IMMIGRATE_PORT`, and
  `IMMIGRATE_HOST`, so rename them in your `.env`. `compose.yaml` now uses an
  `immigrate-data` volume. Copy your old volume into it before the first sync.
  Without its records, Immigrate sends every item again. Immich keeps one copy of
  each, but the files still cross the network. In the folder that holds
  `compose.yaml`, with `photobridge` being that folder's name:

  ```bash
  docker compose down --remove-orphans
  docker compose up -d && docker compose stop
  docker run --rm -v photobridge_photobridge-data:/from -v photobridge_immigrate-data:/to \
    python:3.13-slim cp -a /from/. /to/
  docker compose start
  ```
- The README explains how to install on TrueNAS SCALE and in Portainer, and shows
  the result of a real 27,309-item sync.
- The page uses a near-black theme with an orange accent. It no longer follows the
  system's light mode.
- The image is published to GitHub Container Registry for `amd64` and `arm64`, and
  `compose.yaml` pulls it instead of building. `compose.dev.yaml` builds from a checkout.
- The time zone is chosen in Settings and used for times on the page and in the log.
  `TZ` is no longer passed by `compose.yaml`. If it's set, it's used until you choose one.
- The README explains how to set `NAS_FOLDER` and the user on common NAS systems.
- A library's page offers Delete temporary files when a read that didn't finish left a
  copy of the Photos database in `/data/tmp`. Finished reads now delete their copy straight away.
- Sync more than one library. Each library keeps its own choices, schedule, and records.
  Records from earlier versions move into the first library on start.
- A libraries page shows each library's last sync, its schedule, and when it was last
  checked. Start or stop a sync from there.
- Each library has a page with its sync progress, its check result, and its settings,
  which can be changed and saved after the first sync.
- Settings holds the Immich connection and the defaults for new libraries.
- Setup is split into Connect to Immich, Choose library, Read library, and Choose what to
  send. The schedule is chosen with the other choices.
- Start sync reads the library again before sending.
- Fixed: the container no longer fails to start when `IMMIGRATE_UID` isn't 1000.
- Fixed: every upload failed with "Unsupported file type". Immich checked the sidecar
  against the photo's name, so uploads no longer send the separate `filename` field.
- The library read is saved in `/data`, so it's still there after a restart or rebuild.
- The log now shows when a library read starts, and its progress every 30 seconds.
- Review lists the items whose originals aren't on the NAS, with why and where each
  was expected. The iCloud advice only appears for items that are in iCloud.
- Choose the library in the page by opening folders on the NAS. `compose.yaml` now
  mounts `NAS_FOLDER` (default `/mnt`) at `/nas`. `PHOTOS_LIBRARY` is no longer used.

## 0.1.0

First pre-release.

- Web wizard: connect, review, choose, sync, and check.
- Direct upload from a read-only library mount to Immich v3.
- Live Photos, RAW pairs, edits, albums, favorites, and XMP sidecar metadata.
- Resumable, incremental sync with an optional schedule.
- Verification against what Immich reports.
