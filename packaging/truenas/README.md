# TrueNAS catalog app (draft)

`immigrate/` is a draft app for the community train of the
[TrueNAS apps catalog](https://github.com/truenas/apps). It's laid out like an app
in that repo, so it can be copied to `ix-dev/community/immigrate/` in a fork. It
follows the catalog's `CONTRIBUTIONS.md` and recent community apps such as
`stlquest` and `actual-budget`, and targets library 2.3.18.

## What's left before submitting

1. **Publish a version of the image.** Nothing is at
   `ghcr.io/filipeamorimdev/immigrate` yet. Push a `v0.1.0` tag so the release
   workflow publishes `0.1.0`, and make the package public. Then pin it in
   `ix_values.yaml` with its digest, as other apps do: `0.1.0@sha256:...`.
2. **Make the GitHub repo public under the name `immigrate`.**
   `filipeamorimdev/immigrate` exists but is private. `app.yaml`, the README,
   and the changelog link all point to it.
3. **Prepare an icon and screenshots.** Attach them to the PR. A reviewer uploads
   them to the TrueNAS CDN and gives you the URLs for `icon` and `screenshots` in
   `app.yaml` and `item.yaml`. The icon URL in `app.yaml` is a placeholder in
   their usual pattern.

## When you submit

- Open an issue in `truenas/apps` first, as the PR template asks.
- In the fork, run `devbox run copy-lib`, then test both value files:
  `./.github/scripts/ci.py --app immigrate --train community --test-file basic-values.yaml`.
  Also run `immich-values.yaml`. This draft hasn't been rendered or deployed with
  their scripts yet.
- Run `./.github/scripts/port_validation.py`. `30553` was the next free default
  port when this was drafted. If another app took it, use the next free one it
  prints.
- Update `date_added` in `app.yaml` and `lib_version` to the newest library.
  `copy-lib` updates `lib_version_hash`, and `generate_metadata.py` regenerates
  `item.yaml`.
- Tick the box in the PR template that says an LLM helped write it.

## How it differs from `compose.yaml`

- **The port.** The catalog needs a default port that no other app uses, so it's
  `30553`, not `8088`. The template sets `IMMIGRATE_PORT` to the chosen port, and
  the health check and the web UI link use it.
- **The library folder must be a dataset, such as `/mnt/tank/photos`.** The
  catalog doesn't allow `/mnt` itself to be mounted. The folder is always mounted
  read-only at `/nas`. The permissions step only runs for `/data`, never for the
  library. The ACL options are left out for the library folder, because they would
  change the folder's permissions.
- **Security.** The catalog's library drops all capabilities and sets
  no-new-privileges for every container. The template makes the root filesystem
  read-only, mounts a tmpfs at `/run`, and sets `HOME` to `/data` and `TMPDIR`
  to `/data/tmp`.
- **The form.** The Immich URL, API key (a private field), and log level are
  optional. The time zone sets `TZ`, which is used until one is chosen in the page.
  Labels use title case to match the rest of the TrueNAS form.
