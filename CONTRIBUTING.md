# Contributing

## Setup

You need Python 3.12 or later, Git, and Make. Docker is only needed for image builds
and `make lock`.

```bash
make setup     # creates .venv and installs the project with dev tools
make samples   # downloads two sample Photos libraries into .samples/
make check     # runs lint, type checks, and tests
```

## Run it locally

```bash
make run                      # browses the sample libraries
make run FOLDER=/path/to/folder # browses another folder
```

Then open <http://127.0.0.1:8088> and choose a library under Connect. State is stored in `./data`.

To run your checkout in Docker instead of the published image:

```bash
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

## Everyday commands

| Command | What it does |
| --- | --- |
| `make format` | Applies automatic lint fixes and formatting |
| `make lint` | Checks lint and formatting |
| `make types` | Runs mypy in strict mode |
| `make test` | Runs tests with coverage |
| `make lock` | Rebuilds `requirements.lock` after a dependency change, in the image's base so it suits Linux |
| `make image` | Builds the Docker image |

## Rules of the project

- Sync is one-way and only adds. Code must never write to the library or delete
  anything from Immich.
- Never log, return, or display the API key.
- When Immich's API changes, update `tests/mock_immich.py` first, using the
  OpenAPI spec of the Immich version you're targeting.
- A new behavior gets a test, and a bug fix gets a test that fails without it.

## Releases

1. Update `__version__` in `src/immigrate/__init__.py` and `CHANGELOG.md`.
2. Tag the commit, for example `git tag v0.2.0`. The tag must match `__version__`.
3. Push the tag. GitHub Actions runs the checks, then publishes
   `ghcr.io/<owner>/immigrate` for `amd64` and `arm64`, tagged `0.2.0`, `0.2`, and
   `latest`. A pre-release tag such as `v0.2.0-rc.1` only gets its own name.
4. After the first release, open the package on GitHub, then Package settings, and
   change its visibility to public. GitHub makes new packages private.
