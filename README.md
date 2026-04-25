# music-liked-sync

Bidirectional sync for Spotify liked songs and YouTube Music liked songs.

The tool is conservative by design:

- Dry-run by default; `--apply` is required before it changes either library.
- Matches by normalized title + artist, then fuzzy title/artist/duration.
- Unmatched tracks are reported and skipped.
- Writes a JSON report, default `sync-report.json`.
- Processes all missing tracks by default, using configurable batches and pauses to reduce rate-limit risk.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Spotify Developer app credentials
- Google Cloud OAuth client for YouTube Data API, type **TVs and Limited Input devices**

## Install

```bash
git clone https://github.com/rabilrbl/music-liked-sync.git
cd music-liked-sync
uv sync --all-groups
```

## Spotify auth setup

Yes, Spotify OAuth is supported.

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

You can also pass credentials explicitly:

```bash
uv run python src/music_liked_sync.py \
  --spotify-auth oauth \
  --spotify-client-id '<spotify-client-id>' \
  --spotify-client-secret '<spotify-client-secret>' \
  --spotify-redirect-uri 'http://127.0.0.1:8888/callback'
```

## YouTube Music auth setup

Create a Google Cloud OAuth client for YouTube Data API, application type **TVs and Limited Input devices**, then run:

```bash
mkdir -p auth
uv run ytmusicapi oauth \
  --file auth/oauth.json \
  --client-id '<youtube-client-id>' \
  --client-secret '<youtube-client-secret>'
```

Keep these exports for future runs:

```bash
export YTMUSIC_CLIENT_ID='<youtube-client-id>'
export YTMUSIC_CLIENT_SECRET='<youtube-client-secret>'
```

`auth/` is gitignored because it contains tokens.

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
--spotify-auth {auto,oauth,hermes}
--spotify-client-id VALUE
--spotify-client-secret VALUE
--spotify-redirect-uri VALUE
--spotify-cache PATH
--oauth PATH                    # YouTube Music ytmusicapi oauth.json
--yt-client-id VALUE
--yt-client-secret VALUE
--market IN
--batch-size 50                # tracks to process before pausing
--batch-delay 1.0              # seconds to sleep between batches
--max-add VALUE                # optional cap per direction; omitted means all missing tracks
--report sync-report.json
--apply
```

`--spotify-auth hermes` is only for local Hermes Agent users who already have Hermes Spotify auth configured.
Most users should use `--spotify-auth oauth` or default `auto`.

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run pytest -q
uv run python -m py_compile src/music_liked_sync.py
```

## License

MIT
