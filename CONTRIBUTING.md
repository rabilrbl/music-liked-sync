# Contributing

Thanks for considering a contribution.

## Quick checks

- `uv sync --all-groups`
- `uv run ruff check .`
- `uv run pytest -q`
- `uv run python -m py_compile src/music_liked_sync/*.py`

## Submitting

1. Open an issue first for anything non-trivial.
2. Branch from `main`.
3. Keep changes focused — one concern per PR.
4. Ensure CI passes before requesting review.

## Auth model

Auth is intentionally browser-session only. Do not add static header-file modes or OAuth app selectors — these were removed by design.