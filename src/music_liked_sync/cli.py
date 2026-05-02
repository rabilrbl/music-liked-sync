import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .cache import SyncCache
from .constants import (
    DEFAULT_BATCH_DELAY,
    DEFAULT_BATCH_SIZE,
    DEFAULT_CACHE_DB,
    DEFAULT_LIBRARY_CACHE_TTL,
    DEFAULT_MARKET,
    DEFAULT_SPOTIFY_WEB_LOCK_FILE,
    DEFAULT_SPOTIFY_WEB_SESSION_DIR,
    DEFAULT_YT_BROWSER_LOCK_FILE,
    DEFAULT_YT_BROWSER_SESSION_DIR,
)
from .pipeline import SyncPipeline
from .spotify import SpotifyBackend
from .ytmusic import YTMusicBackend, ensure_yt_browser_auth_from_session


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Spotify and YouTube Music liked songs")
    parser.add_argument("--market", default=os.environ.get("MUSIC_SYNC_MARKET", DEFAULT_MARKET), help="Spotify market code used for search/library reads")
    parser.add_argument("--apply", action="store_true", help="actually save/like matched tracks; default is dry-run")
    parser.add_argument("--max-add", type=positive_int, default=None, help="optional cap on tracks to add per direction; default processes all missing tracks")
    parser.add_argument("--batch-size", type=positive_int, default=int(os.environ.get("MUSIC_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE)), help="tracks to search/save before pausing; Spotify writes are capped to 50 by API")
    parser.add_argument("--batch-delay", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_BATCH_DELAY", DEFAULT_BATCH_DELAY)), help="seconds to sleep between batches")
    parser.add_argument("--cache-db", default=os.environ.get("MUSIC_SYNC_CACHE_DB", DEFAULT_CACHE_DB), help="sqlite path for persistent sync cache")
    parser.add_argument("--cache-library-ttl", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_LIBRARY_CACHE_TTL", DEFAULT_LIBRARY_CACHE_TTL)), help="seconds to reuse cached liked libraries; 0 disables library reuse")
    parser.add_argument("--no-cache-read", dest="cache_read", action="store_false", help="disable reading cached matches/library")
    parser.add_argument("--no-cache-write", dest="cache_write", action="store_false", help="disable writing cache")
    parser.add_argument("--spotify-to-ytm", action="store_true", help="only sync Spotify liked songs into YouTube Music")
    parser.add_argument("--ytm-to-spotify", action="store_true", help="only sync YouTube Music liked songs into Spotify")
    parser.add_argument("--report", default="sync-report.json", help="write JSON report here")
    parser.add_argument("--workers", type=positive_int, default=int(os.environ.get("MUSIC_SYNC_WORKERS", "4")), help="concurrency for searches and library fetching")
    parser.add_argument("--verbose", action="store_true", help="enable detailed logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    yt_browser_session_dir = Path(DEFAULT_YT_BROWSER_SESSION_DIR).expanduser()
    if not yt_browser_session_dir.is_absolute():
        yt_browser_session_dir = Path.cwd() / yt_browser_session_dir

    spotify_web_session_dir = Path(DEFAULT_SPOTIFY_WEB_SESSION_DIR).expanduser()
    if not spotify_web_session_dir.is_absolute():
        spotify_web_session_dir = Path.cwd() / spotify_web_session_dir

    try:
        yt_auth_headers = ensure_yt_browser_auth_from_session(
            session_dir=yt_browser_session_dir,
            lock_path=DEFAULT_YT_BROWSER_LOCK_FILE,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        spotify = SpotifyBackend(
            market=args.market,
            web_session_dir=spotify_web_session_dir,
            lock_path=DEFAULT_SPOTIFY_WEB_LOCK_FILE,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        ytm = YTMusicBackend(yt_auth_headers)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cache_path = Path(args.cache_db).expanduser()
    if not cache_path.is_absolute():
        cache_path = Path.cwd() / cache_path

    report_path = Path(args.report).expanduser()
    if not report_path.is_absolute():
        report_path = Path.cwd() / report_path

    do_spotify_to_ytm = args.spotify_to_ytm or not args.ytm_to_spotify
    do_ytm_to_spotify = args.ytm_to_spotify or not args.spotify_to_ytm

    cache = SyncCache(cache_path)
    try:
        pipeline = SyncPipeline(
            spotify=spotify,
            ytm=ytm,
            cache=cache,
            spotify_to_ytm=do_spotify_to_ytm,
            ytm_to_spotify=do_ytm_to_spotify,
            apply=args.apply,
            max_add=args.max_add,
            batch_size=args.batch_size,
            batch_delay=args.batch_delay,
            workers=args.workers,
            verbose=args.verbose,
            cache_read=args.cache_read,
            cache_write=args.cache_write,
            cache_library_ttl=args.cache_library_ttl,
            report_path=report_path,
        )
        return pipeline.run()
    finally:
        cache.close()
