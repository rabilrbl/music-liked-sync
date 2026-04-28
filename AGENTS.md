# AGENTS.md — music-liked-sync

## Agent must-knows

- **Toolchain:** `uv` only. Python 3.13+, `uv sync --all-groups` to install.
- **Dry-run by default.** The CLI does nothing destructive unless `--apply` is passed. Always verify whether `--apply` is appropriate before suggesting it.
- **Browser session auth only.** Both Spotify and YouTube Music use persistent Chromium profiles. No auth-mode flags exist; auth selector CLI flags were intentionally removed. Do not reintroduce them.
- **Playwright Chromium is required** for real runs: `uv run playwright install chromium`.
- **Tests mock everything.** No browser or real credentials are needed to run the test suite.

## Developer commands

| Command | Purpose |
|---------|---------|
| `uv sync --all-groups` | Install deps + dev deps |
| `uv run playwright install chromium` | Install browser for real runs |
| `uv run music-liked-sync --help` | CLI help |
| `uv run ruff check .` | Lint |
| `uv run pytest -q` | Run tests |
| `uv run pytest tests/test_cli.py -q` | Run a single test file |
| `uv run python -m py_compile src/music_liked_sync/*.py` | CI compile check |

## Architecture

- **Entry:** `src/music_liked_sync/cli.py::main`, exposed as `music-liked-sync` console script and `python -m music_liked_sync`.
- **Package:** Single package `music_liked_sync` under `src/`.
- **Auth stores:** `auth/spotify-web-session/` and `auth/ytmusic-browser-session/` (Chromium profiles). **Never commit `auth/`** — it contains session cookies and is gitignored.
- **Locks:** `state/locks/spotify-web-session.lock` and `state/locks/ytmusic-browser-session.lock` guard browser sessions against concurrent runs.
- **Cache:** SQLite at `state/sync-cache.sqlite3` stores library snapshots and cross-service match mappings.
- **Report:** Writes `sync-report.json` by default (configurable via `--report`).

## Defaults & conventions

- Market default is `"IN"` unless `MUSIC_SYNC_MARKET` is set.
- Batch size defaults to `50`; batch delay defaults to `1.0` seconds.
- Library cache TTL defaults to `0.0` (disabled). Set `--cache-library-ttl` to reuse liked-library snapshots.
- `MUSIC_SYNC_BATCH_SIZE`, `MUSIC_SYNC_BATCH_DELAY`, `MUSIC_SYNC_WORKERS`, `MUSIC_SYNC_CACHE_DB`, `MUSIC_SYNC_LIBRARY_CACHE_TTL` are respected via env.
- Ruff target: `py313`, line length `120`.

## CI pipeline order

1. `uv sync --all-groups`
2. `uv run ruff check .`
3. `uv run pytest -q`
4. `uv run python -m py_compile src/music_liked_sync/*.py`
