# music-liked-sync

Bidirectional sync for Spotify liked songs and YouTube Music liked songs.

Defaults are safe and session-first:
- Dry-run by default (`--apply` required to write)
- Session auth only
  - Spotify: `web-session` (with `auto` accepted and coerced to `web-session`)
  - YouTube Music: `browser` or `browser-session`
- Conservative matching (title + artist normalization, then fuzzy fallback)
- Writes JSON report (`sync-report.json`)
- Persists sqlite cache (`state/sync-cache.sqlite3`) for matches/liked-state reuse

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Chromium via Playwright (`uv run playwright install chromium`)
- Active Spotify and YouTube Music account sessions in browser on first run

## Install

```bash
git clone https://github.com/rabilrbl/music-liked-sync.git
cd music-liked-sync
uv sync --all-groups
uv run playwright install chromium
```

## Auth model (session only)

## Spotify (`--spotify-auth web-session`)

First run opens `https://open.spotify.com` in Chromium. Log in once.
A persistent browser profile is stored at `auth/spotify-web-session/`.
Future runs reuse the profile and mint short-lived web access tokens.

You can set Spotify session defaults in `auth/spotify.json` (gitignored):

```json
{
  "web_session_dir": "auth/spotify-web-session",
  "web_headless": false,
  "web_login_timeout": 300
}
```

## YouTube Music (`--yt-auth browser-session` preferred)

First run opens `https://music.youtube.com` in Chromium. Log in once.
A persistent browser profile is stored at `auth/ytmusic-browser-session/`.
The script generates/refreshes `auth/browser.json` from that session.

```bash
uv run python src/music_liked_sync.py --yt-auth browser-session
```

To force-refresh `auth/browser.json` from the stored browser session:

```bash
uv run python src/music_liked_sync.py --yt-auth browser-session --yt-refresh-browser-auth
```

Manual browser-header mode remains available:

```bash
uv run ytmusicapi browser --file auth/browser.json
uv run python src/music_liked_sync.py --yt-auth browser --yt-auth-file auth/browser.json
```

`auth/` is gitignored because it contains sensitive session/cookie data.

## Usage

## Dry-run (default)

```bash
uv run python src/music_liked_sync.py
```

## Apply bidirectional sync

```bash
uv run python src/music_liked_sync.py --apply
```

## Direction-specific

```bash
# Spotify -> YouTube Music
uv run python src/music_liked_sync.py --spotify-to-ytm --apply

# YouTube Music -> Spotify
uv run python src/music_liked_sync.py --ytm-to-spotify --apply
```

## Batching and caps

```bash
# smaller batches + longer pause
uv run python src/music_liked_sync.py --batch-size 25 --batch-delay 2 --apply

# optional per-direction cap
uv run python src/music_liked_sync.py --max-add 100 --apply
```

## Cache controls

```bash
# custom sqlite path
uv run python src/music_liked_sync.py --cache-db state/my-sync.sqlite3

# reuse cached library snapshots for 30m
uv run python src/music_liked_sync.py --cache-library-ttl 1800

# disable cache read/write
uv run python src/music_liked_sync.py --no-cache-read --no-cache-write
```

## CLI options

```text
--yt-auth {browser,browser-session}
--yt-auth-file PATH
--yt-browser-session-dir PATH
--yt-browser-headless
--yt-browser-login-timeout FLOAT
--yt-refresh-browser-auth

--spotify-auth {auto,web-session}
--spotify-web-session-dir PATH
--spotify-web-headless
--spotify-web-login-timeout FLOAT

--market IN
--apply
--max-add INT
--batch-size INT
--batch-delay FLOAT

--cache-db PATH
--cache-library-ttl FLOAT
--no-cache-read
--no-cache-write

--heartbeat-command CMD
--heartbeat-interval FLOAT
--heartbeat-timeout FLOAT

--spotify-to-ytm
--ytm-to-spotify
--report sync-report.json
```

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest -q
uv run python -m py_compile src/music_liked_sync.py
```

## License

MIT
