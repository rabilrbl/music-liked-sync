#!/usr/bin/env python3
"""Bidirectional sync for Spotify and YouTube Music liked songs.

Default mode is a safe dry-run. Use --apply to actually like/save matches.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from collections.abc import Callable
from typing import Iterable, Sequence

COMMON_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?\(?\b(?:remaster(?:ed)?(?:\s*\d{2,4})?|\d{4}\s*remaster(?:ed)?|"
    r"deluxe(?:\s+edition)?|expanded(?:\s+edition)?|explicit|clean|single version|album version|"
    r"radio edit|edit|live|mono|stereo|from .*|official audio|official video)\b\)?\s*$",
    re.IGNORECASE,
)
SPOTIFY_SCOPES = "user-library-read user-library-modify"
DEFAULT_MARKET = "IN"
DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_DELAY = 1.0


@dataclass(frozen=True)
class Track:
    title: str
    artists: tuple[str, ...]
    source_id: str
    duration_ms: int | None = None
    album: str | None = None

    @property
    def display(self) -> str:
        artist = ", ".join(self.artists) if self.artists else "Unknown Artist"
        return f"{self.title} — {artist}"


def _ascii_lower(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.lower()


def normalize_text(value: str) -> str:
    text = _ascii_lower(value)
    text = text.replace("&", " and ")
    text = re.sub(r"\b(feat|ft|featuring)\.?\b.*$", "", text)
    previous = None
    while previous != text:
        previous = text
        text = COMMON_TITLE_SUFFIX_RE.sub("", text)
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_artist(value: str) -> str:
    text = normalize_text(value)
    # Artist names often differ only by spacing.
    return text.replace(" ", "")


def normalize_key(title: str, artists: Sequence[str]) -> str:
    artist_part = "+".join(sorted(normalize_artist(a) for a in artists if a))
    return f"{normalize_text(title)}::{artist_part}"


def artist_matches(left: Sequence[str], right: Sequence[str]) -> bool:
    left_norm = [normalize_artist(a) for a in left if a]
    right_norm = [normalize_artist(a) for a in right if a]
    if not left_norm or not right_norm:
        return False
    for left_artist in left_norm:
        for right_artist in right_norm:
            if left_artist == right_artist or left_artist in right_artist or right_artist in left_artist:
                return True
            if SequenceMatcher(None, left_artist, right_artist).ratio() >= 0.86:
                return True
    return False


def track_similarity(wanted: Track, candidate: Track) -> float:
    title_score = SequenceMatcher(None, normalize_text(wanted.title), normalize_text(candidate.title)).ratio()
    artist_score = 1.0 if artist_matches(wanted.artists, candidate.artists) else 0.0
    duration_score = 0.0
    if wanted.duration_ms and candidate.duration_ms:
        delta = abs(wanted.duration_ms - candidate.duration_ms)
        duration_score = max(0.0, 1.0 - delta / 30000)  # full credit within ~0s, none after 30s
    return (title_score * 0.62) + (artist_score * 0.33) + (duration_score * 0.05)


def best_match(wanted: Track, candidates: Sequence[Track], threshold: float = 0.82) -> Track | None:
    if not candidates:
        return None
    wanted_key = normalize_key(wanted.title, wanted.artists)
    for candidate in candidates:
        if normalize_key(candidate.title, candidate.artists) == wanted_key:
            return candidate
    scored = sorted(((track_similarity(wanted, c), c) for c in candidates), key=lambda item: item[0], reverse=True)
    score, candidate = scored[0]
    if score >= threshold and artist_matches(wanted.artists, candidate.artists):
        return candidate
    return None


def parse_spotify_track(item: dict) -> Track | None:
    track = item.get("track", item) or {}
    if not track.get("id") or not track.get("name"):
        return None
    artists = tuple(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
    return Track(
        title=track["name"],
        artists=artists,
        source_id=f"spotify:track:{track['id']}",
        duration_ms=track.get("duration_ms"),
        album=(track.get("album") or {}).get("name"),
    )


def parse_ytm_track(item: dict) -> Track | None:
    video_id = item.get("videoId") or item.get("entityId")
    title = item.get("title")
    if not video_id or not title:
        return None
    artists_raw = item.get("artists") or []
    artists: list[str] = []
    for artist in artists_raw:
        if isinstance(artist, dict) and artist.get("name"):
            artists.append(artist["name"])
        elif isinstance(artist, str):
            artists.append(artist)
    duration_ms = None
    if item.get("duration_seconds") is not None:
        duration_ms = int(item["duration_seconds"]) * 1000
    return Track(title=title, artists=tuple(artists), source_id=video_id, duration_ms=duration_ms, album=(item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else None)


def unique_by_key(tracks: Iterable[Track]) -> dict[str, Track]:
    out: dict[str, Track] = {}
    for track in tracks:
        out.setdefault(normalize_key(track.title, track.artists), track)
    return out


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


def batched(items: Sequence, batch_size: int) -> list[Sequence]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [items[start : start + batch_size] for start in range(0, len(items), batch_size)]


def sleep_between_batches(
    batch_index: int,
    total_batches: int,
    batch_delay: float,
    sleep_fn: Callable[[float], None],
) -> None:
    if batch_delay > 0 and batch_index < total_batches - 1:
        sleep_fn(batch_delay)


class SpotifyBackend:
    def __init__(
        self,
        *,
        auth_mode: str = "auto",
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        cache_path: str | None = None,
        market: str = DEFAULT_MARKET,
    ) -> None:
        self.market = market
        self.mode = self._resolve_auth_mode(auth_mode, client_id, client_secret)
        if self.mode == "hermes":
            sys.path.insert(0, "/var/home/rabil/.hermes/hermes-agent")
            from plugins.spotify.client import SpotifyClient  # type: ignore

            self.client = SpotifyClient()
            return

        from spotipy import Spotify
        from spotipy.oauth2 import SpotifyOAuth

        self.client = Spotify(
            auth_manager=SpotifyOAuth(
                client_id=client_id or os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID"),
                client_secret=client_secret
                or os.environ.get("SPOTIPY_CLIENT_SECRET")
                or os.environ.get("SPOTIFY_CLIENT_SECRET"),
                redirect_uri=redirect_uri
                or os.environ.get("SPOTIPY_REDIRECT_URI")
                or os.environ.get("SPOTIFY_REDIRECT_URI")
                or "http://127.0.0.1:8888/callback",
                scope=SPOTIFY_SCOPES,
                cache_path=cache_path or os.environ.get("SPOTIFY_TOKEN_CACHE") or ".cache-spotify",
                open_browser=True,
            )
        )

    @staticmethod
    def _resolve_auth_mode(auth_mode: str, client_id: str | None, client_secret: str | None) -> str:
        if auth_mode != "auto":
            return auth_mode
        has_spotify_oauth = bool(
            (client_id or os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID"))
            and (client_secret or os.environ.get("SPOTIPY_CLIENT_SECRET") or os.environ.get("SPOTIFY_CLIENT_SECRET"))
        )
        if has_spotify_oauth:
            return "oauth"
        if Path("/var/home/rabil/.hermes/hermes-agent/plugins/spotify/client.py").exists():
            return "hermes"
        return "oauth"

    def liked_tracks(self) -> list[Track]:
        tracks: list[Track] = []
        offset = 0
        while True:
            if self.mode == "hermes":
                page = self.client.get_saved_tracks(limit=50, offset=offset, market=self.market)
            else:
                page = self.client.current_user_saved_tracks(limit=50, offset=offset, market=self.market)
            items = page.get("items", []) or []
            for item in items:
                parsed = parse_spotify_track(item)
                if parsed:
                    tracks.append(parsed)
            offset += len(items)
            if not items or offset >= int(page.get("total") or offset):
                break
        return tracks

    def search_track(self, wanted: Track, limit: int = 5) -> list[Track]:
        query = f"track:{wanted.title} artist:{' '.join(wanted.artists[:1])}" if wanted.artists else wanted.title
        if self.mode == "hermes":
            page = self.client.search(query=query, search_types=["track"], limit=limit, market=self.market)
        else:
            page = self.client.search(q=query, type="track", limit=limit, market=self.market)
        items = ((page.get("tracks") or {}).get("items") or [])
        return [t for t in (parse_spotify_track(item) for item in items) if t]

    def save_tracks(
        self,
        tracks: Sequence[Track],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_delay: float = DEFAULT_BATCH_DELAY,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        ids = [track.source_id.split(":")[-1] for track in tracks]
        effective_batch_size = min(batch_size, 50)  # Spotify save-tracks endpoint accepts max 50 IDs.
        chunks = batched(ids, effective_batch_size)
        for index, chunk in enumerate(chunks):
            if self.mode == "hermes":
                self.client.request("PUT", "/me/tracks", params={"ids": ",".join(chunk)})
            else:
                self.client.current_user_saved_tracks_add(tracks=chunk)
            sleep_between_batches(index, len(chunks), batch_delay, sleep_fn)


class YTMusicBackend:
    def __init__(
        self,
        auth_path: Path,
        client_id: str | None,
        client_secret: str | None,
        auth_mode: str = "auto",
    ) -> None:
        from ytmusicapi import YTMusic
        from ytmusicapi.auth.oauth import OAuthCredentials

        if not auth_path.exists():
            raise FileNotFoundError(f"YouTube Music auth file missing: {auth_path}")

        self.mode = self.resolve_auth_mode(auth_mode, auth_path, client_id, client_secret)
        creds = None
        if self.mode == "oauth" and client_id and client_secret:
            creds = OAuthCredentials(client_id=client_id, client_secret=client_secret)
        self.client = YTMusic(str(auth_path), oauth_credentials=creds)

    @staticmethod
    def resolve_auth_mode(
        auth_mode: str,
        auth_path: Path,
        client_id: str | None,
        client_secret: str | None,
    ) -> str:
        if auth_mode != "auto":
            return auth_mode
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            keys = {str(key).lower() for key in data}
            if {"cookie", "authorization"} <= keys:
                return "browser"
            if "refresh_token" in keys or "access_token" in keys:
                return "oauth"
        if client_id and client_secret:
            return "oauth"
        return "browser"

    def liked_tracks(self, limit: int | None = None) -> list[Track]:
        result = self.client.get_liked_songs(limit=limit or 10000)
        items = result.get("tracks", []) if isinstance(result, dict) else result
        return [t for t in (parse_ytm_track(item) for item in (items or [])) if t]

    def search_track(self, wanted: Track, limit: int = 5) -> list[Track]:
        query = f"{wanted.title} {' '.join(wanted.artists)}".strip()
        items = self.client.search(query, filter="songs", limit=limit) or []
        return [t for t in (parse_ytm_track(item) for item in items) if t]

    def like_tracks(
        self,
        tracks: Sequence[Track],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        batch_delay: float = DEFAULT_BATCH_DELAY,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        chunks = batched(tracks, batch_size)
        for index, chunk in enumerate(chunks):
            for track in chunk:
                self.client.rate_song(track.source_id, "LIKE")
            sleep_between_batches(index, len(chunks), batch_delay, sleep_fn)


def compute_missing(left: Sequence[Track], right: Sequence[Track]) -> list[Track]:
    right_keys = set(unique_by_key(right))
    return [track for track in left if normalize_key(track.title, track.artists) not in right_keys]


def resolve_matches(
    missing: Sequence[Track],
    search_fn,
    max_add: int | None,
    label: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_delay: float = DEFAULT_BATCH_DELAY,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[tuple[Track, Track]], list[Track]]:
    matched: list[tuple[Track, Track]] = []
    unmatched: list[Track] = []
    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    chunks = batched(candidates_to_process, batch_size)
    for batch_index, chunk in enumerate(chunks):
        for wanted in chunk:
            candidates = search_fn(wanted)
            match = best_match(wanted, candidates)
            if match:
                matched.append((wanted, match))
            else:
                unmatched.append(wanted)
            print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
        sleep_between_batches(batch_index, len(chunks), batch_delay, sleep_fn)
    print("".ljust(90), end="\r")
    return matched, unmatched


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync Spotify and YouTube Music liked songs")
    parser.add_argument("--yt-auth", choices=("auto", "oauth", "browser"), default=os.environ.get("YTMUSIC_AUTH", "auto"), help="YouTube Music auth type; auto detects oauth.json vs browser headers JSON")
    parser.add_argument("--yt-auth-file", "--oauth", dest="yt_auth_file", default=os.environ.get("YTMUSIC_AUTH_FILE") or os.environ.get("YTMUSIC_OAUTH", "auth/oauth.json"), help="YouTube Music auth JSON path; --oauth is kept as a backwards-compatible alias")
    parser.add_argument("--yt-client-id", default=os.environ.get("YTMUSIC_CLIENT_ID"))
    parser.add_argument("--yt-client-secret", default=os.environ.get("YTMUSIC_CLIENT_SECRET"))
    parser.add_argument("--spotify-auth", choices=("auto", "oauth", "hermes"), default=os.environ.get("SPOTIFY_AUTH", "auto"), help="Spotify auth backend: oauth uses Spotipy; hermes reuses local Hermes auth if available")
    parser.add_argument("--spotify-client-id", default=os.environ.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIPY_CLIENT_ID"))
    parser.add_argument("--spotify-client-secret", default=os.environ.get("SPOTIFY_CLIENT_SECRET") or os.environ.get("SPOTIPY_CLIENT_SECRET"))
    parser.add_argument("--spotify-redirect-uri", default=os.environ.get("SPOTIFY_REDIRECT_URI") or os.environ.get("SPOTIPY_REDIRECT_URI") or "http://127.0.0.1:8888/callback")
    parser.add_argument("--spotify-cache", default=os.environ.get("SPOTIFY_TOKEN_CACHE", ".cache-spotify"), help="Spotipy OAuth token cache path")
    parser.add_argument("--market", default=os.environ.get("MUSIC_SYNC_MARKET", DEFAULT_MARKET), help="Spotify market code used for search/library reads")
    parser.add_argument("--apply", action="store_true", help="actually save/like matched tracks; default is dry-run")
    parser.add_argument("--max-add", type=positive_int, default=None, help="optional cap on tracks to add per direction; default processes all missing tracks")
    parser.add_argument("--batch-size", type=positive_int, default=int(os.environ.get("MUSIC_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE)), help="tracks to search/save before pausing; Spotify writes are capped to 50 by API")
    parser.add_argument("--batch-delay", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_BATCH_DELAY", DEFAULT_BATCH_DELAY)), help="seconds to sleep between batches")
    parser.add_argument("--spotify-to-ytm", action="store_true", help="only sync Spotify liked songs into YouTube Music")
    parser.add_argument("--ytm-to-spotify", action="store_true", help="only sync YouTube Music liked songs into Spotify")
    parser.add_argument("--report", default="sync-report.json", help="write JSON report here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    auth_path = Path(args.yt_auth_file).expanduser()
    if not auth_path.is_absolute():
        auth_path = Path.cwd() / auth_path

    spotify = SpotifyBackend(
        auth_mode=args.spotify_auth,
        client_id=args.spotify_client_id,
        client_secret=args.spotify_client_secret,
        redirect_uri=args.spotify_redirect_uri,
        cache_path=args.spotify_cache,
        market=args.market,
    )
    try:
        ytm = YTMusicBackend(auth_path, args.yt_client_id, args.yt_client_secret, auth_mode=args.yt_auth)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "Run one of: uv run ytmusicapi oauth --file auth/oauth.json "
            "--client-id '<CLIENT_ID>' --client-secret '<CLIENT_SECRET>'; "
            "or uv run ytmusicapi browser --file auth/browser.json",
            file=sys.stderr,
        )
        return 2

    spotify_liked = spotify.liked_tracks()
    ytm_liked = ytm.liked_tracks()
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
    }

    if do_spotify_to_ytm:
        missing = compute_missing(spotify_liked, ytm_liked)
        matched, unmatched = resolve_matches(
            missing,
            ytm.search_track,
            args.max_add,
            "Spotify → YTM",
            batch_size=args.batch_size,
            batch_delay=args.batch_delay,
        )
        if args.apply:
            ytm.like_tracks(
                [match for _, match in matched],
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
            )
        report["spotify_to_ytm"] = {
            "missing_count": len(missing),
            "matched_count": len(matched),
            "unmatched_count_sampled": len(unmatched),
            "matched": [{"source": asdict(src), "target": asdict(dst)} for src, dst in matched],
            "unmatched": [asdict(track) for track in unmatched],
        }

    if do_ytm_to_spotify:
        missing = compute_missing(ytm_liked, spotify_liked)
        matched, unmatched = resolve_matches(
            missing,
            spotify.search_track,
            args.max_add,
            "YTM → Spotify",
            batch_size=args.batch_size,
            batch_delay=args.batch_delay,
        )
        if args.apply:
            spotify.save_tracks(
                [match for _, match in matched],
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
            )
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
        "spotify_to_ytm": report["spotify_to_ytm"],
        "ytm_to_spotify": report["ytm_to_spotify"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
