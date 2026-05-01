# music-liked-sync

<p align="center">
  <b>Bidirectional sync for Spotify liked songs ↔ YouTube Music liked songs</b><br>
  Dry-run by default • No API keys • Session auth only
</p>

<p align="center">
  <a href="https://pypi.org/project/music-liked-sync/"><img alt="PyPI" src="https://img.shields.io/pypi/v/music-liked-sync.svg"></a>
  <a href="https://github.com/rabilrbl/music-liked-sync/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/rabilrbl/music-liked-sync/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.13%2B-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
</p>

---

## Install

**One-shot (recommended):**

```bash
uvx music-liked-sync --help
```

**Permanent install:**

```bash
uv tool install music-liked-sync
```

**Classic:**

```bash
pip install music-liked-sync
```

---

## What it does

Keeps your liked-songs libraries in sync across Spotify and YouTube Music.

- **Bidirectional** — defaults to both directions; run one-way with flags
- **Dry-run by default** — preview every change before applying (`--apply` required)
- **No API keys** — uses live browser sessions (Playwright) for both services
- **Stateful** — SQLite cache remembers match mappings so re-runs are fast
- **Safe** — lock files prevent concurrent browser sessions; conservative matching

---

## Quick start

### 1. Install

```bash
uv tool install music-liked-sync
uv run playwright install chromium   # first time only
```

### 2. Authenticate (browser session)

On first run the tool opens Chromium for each service. Log in once — sessions persist.

```bash
# Spotify session
music-liked-sync --dry-run   # opens https://open.spotify.com, log in, close

# YouTube Music session
music-liked-sync --dry-run   # opens https://music.youtube.com, log in, close
```

> Session profiles are stored in `auth/spotify-web-session/` and `auth/ytmusic-browser-session/`.  
> `auth/` is `.gitignore`d and should never be committed.

### 3. Preview (dry-run)

```bash
music-liked-sync
```

Shows what would be added in each direction without writing anything.

### 4. Sync

```bash
music-liked-sync --apply
```

---

## Usage

### Direction-specific

```bash
# Spotify → YouTube Music only
music-liked-sync --spotify-to-ytm --apply

# YouTube Music → Spotify only
music-liked-sync --ytm-to-spotify --apply
```

### Batching and limits

```bash
# Smaller batches, slower pace
music-liked-sync --batch-size 25 --batch-delay 2 --apply

# Cap per-direction additions
music-liked-sync --max-add 100 --apply
```

### Cache controls

```bash
# Custom SQLite path
music-liked-sync --cache-db state/my-sync.sqlite3

# Reuse cached library snapshots for 30 minutes
music-liked-sync --cache-library-ttl 1800 --apply

# Disable cache
music-liked-sync --no-cache-read --no-cache-write --apply
```

### Full CLI options

```text
--market IN              Spotify market code (default: IN)
--apply                  Actually save/like matched tracks
--max-add INT            Cap tracks to add per direction
--batch-size INT         Tracks per batch (default: 50)
--batch-delay FLOAT      Seconds between batches (default: 1.0)
--cache-db PATH          SQLite cache path
--cache-library-ttl FLOAT  Library snapshot TTL in seconds
--no-cache-read          Skip reading cached data
--no-cache-write         Skip writing cached data
--spotify-to-ytm         Only Spotify → YouTube Music
--ytm-to-spotify         Only YouTube Music → Spotify
--report PATH            JSON report path (default: sync-report.json)
--workers INT            Concurrency for searches/library fetching
--verbose                Detailed logging
```

---

## How it compares

| | **music-liked-sync** | sigma67/spotify_to_ytmusic | linsomniac/spotify_to_ytmusic |
|---|---|---|---|
| **Direction** | Bidirectional | One-way (Spotify → YTM) | One-way (Spotify → YTM) |
| **Scope** | Liked songs | Playlists + liked songs | Playlists + liked songs |
| **Auth** | Browser session (no keys) | OAuth app required | Manual header extraction |
| **Dry-run** | ✅ Default | ❌ | ❌ |
| **GUI** | ❌ CLI only | ❌ | ✅ |
| **Install** | `uvx` / `pip` | `pip` | Clone + venv |

Use this if you want **bidirectional liked-songs sync without configuring API apps**.

---

## Architecture

| Component | Purpose |
|-----------|---------|
| `music_liked_sync/cli.py` | Entry point (`music-liked-sync` console script) |
| `music_liked_sync/spotify.py` | Spotify Web Player session + liked-library read/write |
| `music_liked_sync/ytmusic.py` | YouTube Music session + liked-library read/write |
| `music_liked_sync/sync.py` | Diff + fuzzy match resolution |
| `music_liked_sync/cache.py` | SQLite persistence for matches and snapshots |
| `auth/` | Persistent Chromium profiles (gitignored) |
| `state/locks/` | File locks preventing concurrent browser sessions |

---

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest -q
uv run music-liked-sync --help
```

## License

MIT © 2026 Rabil