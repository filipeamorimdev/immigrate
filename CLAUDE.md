# Immigrate

One-way sync from an Apple Photos library into Immich, in a Docker container.
Read `docs/architecture.md` before larger changes.

## Commands

- `make setup` and `make samples` prepare a new checkout.
- `make check` must pass before work is done. It runs ruff, strict mypy, and pytest.
- `make format` fixes formatting.

## Rules

- Never write to the Photos library, and never delete anything from Immich.
- Never log, return, or display the API key.
- Target the Immich API version named in `src/immigrate/immich.py`. Check the
  OpenAPI spec rather than assuming an endpoint or field exists, and keep
  `tests/mock_immich.py` in step with it.
- After changing dependencies in `pyproject.toml`, run `make lock`.
- UI copy is plain and in sentence case. Keep button names the same through the flow.
- The frontend is plain HTML, CSS, and JavaScript with no build step. Fonts stay
  bundled so the page works without internet access.
