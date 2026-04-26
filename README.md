# music-liked-sync

Bidirectional sync for Spotify liked songs and YouTube Music liked songs.

The tool is conservative by design:

- Dry-run by default; `--apply` is required before it changes either library.
- Matches by normalized title + artist, then fuzzy title/artist/duration.
- Unmatched tracks are reported and skipped.
- Writes a JSON report, default `sync-report.json`.
- Stores persistent sync state in sqlite (`state/sync-cache.sqlite3`) to avoid re-searching matches and re-liking already-applied tracks.
- Processes all missing tracks by default, using configurable batches and pauses to reduce rate-limit risk.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Spotify Web Player login, or Spotify Developer app credentials for OAuth/PKCE
- Google Cloud OAuth client for YouTube Data API, or persistent browser-session auth from `music.youtube.com`

## Install

```bash
git clone https://github.com/rabilrbl/music-liked-sync.git
cd music-liked-sync
uv sync --all-groups
```

## Spotify auth setup

### Option A: Persistent Spotify Web Player session

This is the preferred path for long personal sync runs when OAuth/PKCE is rate-limited. It opens a real Chromium window at `open.spotify.com`, you log in once, and the browser profile is reused from `auth/spotify-web-session/`. The script mints short-lived Web Player access tokens from that session; no Spotify app/client ID is required for this mode.

First run:

```bash
uv run playwright install chromium
uv run python src/music_liked_sync.py --spotify-auth web-session
```

Later runs reuse the same browser profile. For local/default runs, you can set this in `auth/spotify.json` (gitignored):

```json
{
  "auth": "web-session",
  "web_session_dir": "auth/spotify-web-session"
}
```

### Option B: Spotify OAuth/PKCE

Spotify OAuth/PKCE is still supported.

Create an app at <https://developer.spotify.com/dashboard>, add this redirect URI:

```text
http://127.0.0.1:8888/callback
```

Then export credentials:

```bash
export SPOTIFY_CLIENT_ID='<spotify-client-id>'
export SPOTIFY_CLIENT_SECRET='<spotify-client-secret>'
export SPOTIFY_REDIRECT_URI='http://127.0.0.1:8888/callback'
```

The first run opens Spotify login and stores a local token in `.cache-spotify`.
Required scopes: `user-library-read user-library-modify`.

For local/default runs, you can put non-secret defaults in `auth/spotify.json` (gitignored):

```json
{
  "auth": "pkce",
  "client_id": "<spotify-client-id>",
  "redirect_uri": "http://127.0.0.1:43827/spotify/callback",
  "cache": ".cache-spotify-pkce"
}
```

Environment variables and CLI flags override this file.

You can also pass credentials explicitly:

```bash
uv run python src/music_liked_sync.py \
  --spotify-auth oauth \
  --spotify-client-id '<spotify-client-id>' \
  --spotify-client-secret '<spotify-client-secret>' \
  --spotify-redirect-uri 'http://127.0.0.1:8888/callback'
```

## YouTube Music auth setup

### Option A: OAuth

Create a Google Cloud OAuth client for YouTube Data API, application type **TVs and Limited Input devices**, then run:

```bash
mkdir -p auth
uv run ytmusicapi oauth \
  --file auth/oauth.json \
  --client-id '<youtube-client-id>' \
  --client-secret '<youtube-client-secret>'
```

Keep these exports for future OAuth runs:

```bash
export YTMUSIC_AUTH=oauth
export YTMUSIC_AUTH_FILE='auth/oauth.json'
export YTMUSIC_CLIENT_ID='<youtube-client-id>'
export YTMUSIC_CLIENT_SECRET='<youtube-client-secret>'
```

If OAuth returns `HTTP 400: Bad Request. Request contains an invalid argument`, use browser auth instead.

### Option B: Persistent browser session

This is the default and the preferred path when YouTube Music rejects OAuth.

First run:

```bash
uv run playwright install chromium
uv run python src/music_liked_sync.py --yt-auth browser-session
```

A real Chromium window opens at `music.youtube.com`. Log in once. The browser profile is stored under `auth/ytmusic-browser-session/`, and `auth/browser.json` is generated from that live session. Later runs reuse the same browser session and do not require pasted request headers or re-login.

Force-refresh `auth/browser.json` from the persisted session:

```bash
uv run python src/music_liked_sync.py --yt-auth browser-session --yt-refresh-browser-auth
```

Manual pasted browser headers still work if needed:

```bash
uv run ytmusicapi browser --file auth/browser.json
uv run python src/music_liked_sync.py --yt-auth browser --yt-auth-file auth/browser.json
```

`auth/` is gitignored because it contains browser profiles, tokens, cookies, and generated auth headers.

## Dry-run

By default, the tool scans and attempts to match **all** missing tracks in both directions.
It works in batches of 50 and sleeps 1 second between batches.

```bash
uv run python src/music_liked_sync.py
```

Tune batching:

```bash
uv run python src/music_liked_sync.py --batch-size 25 --batch-delay 2
```

Use `--max-add` only when you want to cap a run manually:

```bash
uv run python src/music_liked_sync.py --max-add 100
```

## Persistent sqlite cache

By default, sync state is stored at `state/sync-cache.sqlite3`.

This cache stores:
- previously resolved cross-service matches (so next runs skip repeat search calls)
- IDs already liked/saved by this tool (so apply runs skip duplicate write calls)
- optional cached full liked libraries (disabled by default)

Useful flags:

```bash
# custom sqlite path
uv run python src/music_liked_sync.py --cache-db state/my-sync.sqlite3

# reuse cached liked libraries for 30 minutes
uv run python src/music_liked_sync.py --cache-library-ttl 1800

# disable cache reads/writes for a clean run
uv run python src/music_liked_sync.py --no-cache-read --no-cache-write
```

## Apply bidirectional sync

```bash
uv run python src/music_liked_sync.py --apply
```

## Direction-specific sync

```bash
# Spotify liked -> YouTube Music liked only
uv run python src/music_liked_sync.py --spotify-to-ytm --apply

# YouTube Music liked -> Spotify liked only
uv run python src/music_liked_sync.py --ytm-to-spotify --apply
```

## Options

```text
--spotify-auth {auto,oauth,pkce,web-session}
--spotify-client-id VALUE
--spotify-client-secret VALUE
--spotify-redirect-uri VALUE
--spotify-cache PATH
--spotify-web-session-dir auth/spotify-web-session
--spotify-web-headless
--spotify-web-login-timeout 300
--yt-auth {auto,oauth,browser,browser-session}
--yt-auth-file PATH              # YouTube Music oauth.json or browser headers JSON
--oauth PATH                    # backwards-compatible alias for --yt-auth-file
--yt-client-id VALUE            # OAuth mode only
--yt-client-secret VALUE        # OAuth mode only
--market IN
--batch-size 50                # tracks to process before pausing
--batch-delay 1.0              # seconds to sleep between batches
--cache-db state/sync-cache.sqlite3
--cache-library-ttl 0.0        # seconds to reuse cached liked libraries; 0 disables
--no-cache-read
--no-cache-write
--max-add VALUE                # optional cap per direction; omitted means all missing tracks
--report sync-report.json
--apply
```

Most users should use `--spotify-auth web-session` for long personal sync runs, or `--spotify-auth oauth` / `--spotify-auth pkce` for standard Spotify app auth.
Use `pkce` when you only have a Spotify Client ID and no client secret:

```bash
uv run python src/music_liked_sync.py \
  --spotify-auth pkce \
  --spotify-client-id '<spotify-client-id>' \
  --spotify-redirect-uri 'http://127.0.0.1:43827/spotify/callback'
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
