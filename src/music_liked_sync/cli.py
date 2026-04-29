import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
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
    DEFAULT_YT_BROWSER_SESSION_DIR,
    DEFAULT_YT_BROWSER_LOCK_FILE,
)
from .spotify import SpotifyBackend
from .sync import compute_missing, resolve_matches
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

    def vprint(*msg, **kwargs):
        if args.verbose:
            print(*msg, **kwargs)

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

    cache_path = Path(args.cache_db).expanduser()
    if not cache_path.is_absolute():
        cache_path = Path.cwd() / cache_path

    try:
        ytm = YTMusicBackend(yt_auth_headers)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cache = SyncCache(cache_path)
    try:
        spotify_liked = cache.get_library("spotify", args.cache_library_ttl) if args.cache_read else None
        if spotify_liked is None:
            spotify_liked = spotify.liked_tracks(max_workers=args.workers, verbose=args.verbose)
            if args.cache_write:
                cache.store_library("spotify", spotify_liked)
        else:
            vprint(f"Loaded {len(spotify_liked)} Spotify tracks from cache")

        ytm_liked = cache.get_library("ytm", args.cache_library_ttl) if args.cache_read else None
        if ytm_liked is None:
            try:
                ytm_liked = ytm.liked_tracks(verbose=args.verbose)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if args.cache_write:
                cache.store_library("ytm", ytm_liked)
        else:
            vprint(f"Loaded {len(ytm_liked)} YouTube Music tracks from cache")

        do_spotify_to_ytm = args.spotify_to_ytm or not args.ytm_to_spotify
        do_ytm_to_spotify = args.ytm_to_spotify or not args.spotify_to_ytm

        report: dict = {
            "apply": args.apply,
            "spotify_liked_count": len(spotify_liked),
            "ytm_liked_count": len(ytm_liked),
            "spotify_to_ytm": {},
            "ytm_to_spotify": {},
            "batch_size": args.batch_size,
            "batch_delay": args.batch_delay,
            "max_add": args.max_add,
            "yt_auth": ytm.mode,
            "yt_browser_session_dir": str(yt_browser_session_dir),
            "spotify_auth": getattr(spotify, "mode", "web-session"),
            "spotify_web_session_dir": str(spotify_web_session_dir),
            "cache_db": str(cache_path),
            "cache_read": bool(args.cache_read),
            "cache_write": bool(args.cache_write),
            "cache_library_ttl": args.cache_library_ttl,
        }

        if do_spotify_to_ytm:
            missing = compute_missing(spotify_liked, ytm_liked, verbose=args.verbose)
            vprint(f"Spotify → YTM: {len(missing)} tracks missing in YTM")
            matched, unmatched = resolve_matches(
                missing,
                ytm.search_track,
                args.max_add,
                "Spotify → YTM",
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
                cache=cache,
                cache_direction="spotify_to_ytm",
                cache_read=args.cache_read,
                cache_write=args.cache_write,
                max_workers=args.workers,
                verbose=args.verbose,
            )
            if args.apply:
                to_like = [match for _, match in matched]
                if args.cache_read:
                    to_like = [track for track in to_like if not cache.is_liked("ytm", track.source_id)]
                vprint(f"Spotify → YTM: Liking {len(to_like)} tracks on YTM")
                try:
                    ytm.like_tracks(
                        to_like,
                        batch_size=args.batch_size,
                        batch_delay=args.batch_delay,
                        max_workers=args.workers,
                        verbose=args.verbose,
                    )
                except RuntimeError as exc:
                    print(str(exc), file=sys.stderr)
                    return 2
                if args.cache_write:
                    cache.mark_liked_many("ytm", [track.source_id for track in to_like])
            report["spotify_to_ytm"] = {
                "missing_count": len(missing),
                "matched_count": len(matched),
                "unmatched_count_sampled": len(unmatched),
                "matched": [{"source": asdict(src), "target": asdict(dst)} for src, dst in matched],
                "unmatched": [asdict(track) for track in unmatched],
            }

        if do_ytm_to_spotify:
            missing = compute_missing(ytm_liked, spotify_liked, verbose=args.verbose)
            vprint(f"YTM → Spotify: {len(missing)} tracks missing in Spotify")
            matched, unmatched = resolve_matches(
                missing,
                spotify.search_track,
                args.max_add,
                "YTM → Spotify",
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
                cache=cache,
                cache_direction="ytm_to_spotify",
                cache_read=args.cache_read,
                cache_write=args.cache_write,
                max_workers=args.workers,
                verbose=args.verbose,
            )
            if args.apply:
                to_save = [match for _, match in matched]
                if args.cache_read:
                    to_save = [track for track in to_save if not cache.is_liked("spotify", track.source_id)]
                vprint(f"YTM → Spotify: Saving {len(to_save)} tracks to Spotify")
                spotify.save_tracks(
                    to_save,
                    batch_size=args.batch_size,
                    batch_delay=args.batch_delay,
                    max_workers=args.workers,
                    verbose=args.verbose,
                )
                if args.cache_write:
                    cache.mark_liked_many("spotify", [track.source_id for track in to_save])
            report["ytm_to_spotify"] = {
                "missing_count": len(missing),
                "matched_count": len(matched),
                "unmatched_count_sampled": len(unmatched),
                "matched": [{"source": asdict(src), "target": asdict(dst)} for src, dst in matched],
                "unmatched": [asdict(track) for track in unmatched],
            }

        report_path = Path(args.report).expanduser()
        if not report_path.is_absolute():
            report_path = Path.cwd() / report_path
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(json.dumps({
            "apply": args.apply,
            "spotify_liked_count": len(spotify_liked),
            "ytm_liked_count": len(ytm_liked),
            "report": str(report_path),
            "batch_size": args.batch_size,
            "batch_delay": args.batch_delay,
            "max_add": args.max_add,
            "yt_auth": ytm.mode,
            "yt_browser_session_dir": str(yt_browser_session_dir),
            "spotify_auth": getattr(spotify, "mode", "web-session"),
            "spotify_web_session_dir": str(spotify_web_session_dir),
            "cache_db": str(cache_path),
            "cache_read": bool(args.cache_read),
            "cache_write": bool(args.cache_write),
            "cache_library_ttl": args.cache_library_ttl,
        }, ensure_ascii=False, indent=2))
        return 0
    finally:
        cache.close()