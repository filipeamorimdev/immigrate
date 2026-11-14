# TrueNAS catalog app (draft)

`immigrate/` is a draft app for the community train of the
[TrueNAS apps catalog](https://github.com/truenas/apps). It's laid out like an app
in that repo, so it can be copied to `ix-dev/community/immigrate/` in a fork. It
follows the catalog's `CONTRIBUTIONS.md` and recent community apps such as
`stlquest` and `actual-budget`, and targets library 2.3.18.

## Ready

- The image is pinned to `0.4.0` by digest in `ix_values.yaml`, and `app_version`
  is `0.4.0`.
- `lib_version` is 2.3.18, the newest library in the catalog on 2026-11-09, and
  `lib_version_hash` matches its `library/hashes.yaml`.
- The default port is `30560`. The catalog's `port_validation.py` listed it as the
  next free port on 2026-11-09, after `30553` was taken.
- `date_added` is `2026-11-09`.
- The issue and PR text are in [`SUBMISSION.md`](SUBMISSION.md).

## Tested with the catalog's scripts

On 2026-11-09 the app was tested with the catalog's own scripts, in a read-only copy
of `truenas/apps`, against the published 0.2.0 image. These all passed:

- `check_metadata.py`: `app.yaml` and `item.yaml` match what `generate_metadata.py`
  produces.
- `ci.py` with `basic-values.yaml` and with `immich-values.yaml`: the app renders,
  the permissions container finishes, and Immigrate starts and reports healthy.
- `apps_dev_charts_validate validate`, which the catalog's PR checks also run.
- `port_validation.py`: no duplicate ports, and `30560` belongs to Immigrate.

The tests ran on an Apple Silicon Mac, so two things were changed in the copy only,
never in this folder:

- The validation image is amd64-only, and Docker's Rosetta emulation doesn't
  support the `openat2` call the renderer uses to write files. A small fallback
  replaced that one call with a plain open.
- Docker Desktop can't mount `/opt/tests`, and port 8080 was in use, so the test
  value files used a shared temporary folder and ports 18080 and 18081.

On Linux, as in the catalog's CI, neither change is needed.

On 2026-11-14 the pin moved to 0.4.0. The template and its settings didn't change.
The 0.4.0 image was started the way the template runs it: read-only root, a tmpfs
at `/run`, user and group 568, all capabilities dropped, no-new-privileges, port
`30560`, and the library mounted read-only. It reported healthy. The catalog's
scripts haven't been run against 0.4.0 yet, so run them in the fork before the PR.

## When you submit

1. Open the issue in `truenas/apps` with the App Request form, using the text in
   `SUBMISSION.md`.
2. In a fork, copy `immigrate/` to `ix-dev/community/immigrate/` and run
   `devbox run copy-lib`.
3. Test both value files again, in case the catalog changed since:
   `./.github/scripts/ci.py --app immigrate --train community --test-file basic-values.yaml`,
   then the same with `immich-values.yaml`.
4. Run `./.github/scripts/port_validation.py` again. If another app took `30560`,
   use the next free port it prints, and update `questions.yaml` and this README.
5. If you submit on a later day, update `date_added`, and `lib_version` if a newer
   library is out. Run `generate_metadata.py` to regenerate `item.yaml`.
6. Open the PR with the App Addition template and the text in `SUBMISSION.md`.
   Attach the icon (`icon.svg`, with `icon.png` as a 512px copy) and the
   screenshots from `docs/images/`. A reviewer uploads them to the TrueNAS CDN and
   gives you the URLs for `icon` and `screenshots` in `app.yaml` and `item.yaml`.
   The icon URL in `app.yaml` is a placeholder in their usual pattern.

## How it differs from `compose.yaml`

- **The port.** The catalog needs a default port that no other app uses, so it's
  `30560`, not `8088`. The template sets `IMMIGRATE_PORT` to the chosen port, and
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
