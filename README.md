# music-liked-sync

Bidirectional sync for Spotify liked songs and YouTube Music liked songs.

Defaults are safe and session-first:
- Dry-run by default (`--apply` required to write)
- Session auth only
  - Spotify: persistent Spotify Web Player browser session
  - YouTube Music: persistent YouTube Music browser session
- Browser session access is guarded by lock files so only one active browser window/session refresh runs at a time
- Conservative matching (title + artist normalization, then fuzzy fallback)
- Writes JSON report (`sync-report.json`)
- Persists sqlite cache (`state/sync-cache.sqlite3`) for matches/liked-state reuse

## Install

Download the latest binary for your OS and architecture from the [Releases](https://github.com/rabilrbl/music-liked-sync/releases) page.

Alternatively, if you have Go installed:

```bash
go install github.com/rabilrbl/music-liked-sync/cmd/music-liked-sync@latest
```

### Playwright Requirements

This tool uses a headless Chromium browser to authenticate. You must install the Playwright dependencies before your first run.

If you installed via a binary release:
```bash
# The CLI will prompt you if Playwright drivers are missing, typically requiring:
npx playwright install chromium
# Or using the Go package installer directly:
go run github.com/playwright-community/playwright-go/cmd/playwright@latest install --with-deps chromium
```

## Auth model

Auth is intentionally single-path and browser-session only. There is no static YouTube header file (`browser.json`) mode and no Spotify/YT auth-mode selector.

## Spotify

First run opens `https://open.spotify.com` in Chromium. Log in once.
A persistent browser profile is stored at `auth/spotify-web-session/`.
Future runs reuse the profile and mint short-lived web access tokens.

The active Spotify browser session is protected by `state/locks/spotify-web-session.lock`.

## YouTube Music

First run opens `https://music.youtube.com` in Chromium. Log in once.
A persistent browser profile is stored at `auth/ytmusic-browser-session/`.
Future runs build fresh ytmusicapi browser headers directly from that live session; nothing is written to `auth/browser.json`.

The active YouTube Music browser session is protected by `state/locks/ytmusic-browser-session.lock`.

`auth/` is gitignored because it contains sensitive session/cookie data.

## Usage

## Dry-run (default)

```bash
music-liked-sync
```

## Apply bidirectional sync

```bash
music-liked-sync --apply
```

## Direction-specific

```bash
# Spotify -> YouTube Music
music-liked-sync --spotify-to-ytm --apply

# YouTube Music -> Spotify
music-liked-sync --ytm-to-spotify --apply
```

## Batching and caps

```bash
# smaller batches + longer pause
music-liked-sync --batch-size 25 --batch-delay 2 --apply

# optional per-direction cap
music-liked-sync --max-add 100 --apply
```

## Cache controls

```bash
# custom sqlite path
music-liked-sync --cache-db state/my-sync.sqlite3

# reuse cached library snapshots for 30m
music-liked-sync --cache-library-ttl 1800

# disable cache read/write
music-liked-sync --no-cache-read --no-cache-write
```

## CLI options

```text
--market IN
--apply
--max-add INT
--batch-size INT
--batch-delay FLOAT

--cache-db PATH
--cache-library-ttl FLOAT
--no-cache-read
--no-cache-write

--spotify-to-ytm
--ytm-to-spotify
--report sync-report.json
```

## Development

```bash
go mod tidy
gofmt -w .
go test -v ./...
go build -o music-liked-sync ./cmd/music-liked-sync
```

## License

MIT
