#!/usr/bin/env python3
"""Bidirectional sync for Spotify and YouTube Music liked songs.

Default mode is a safe dry-run. Use --apply to actually like/save matches.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from hashlib import sha1
from http.cookies import SimpleCookie
from pathlib import Path
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
DEFAULT_CACHE_DB = "state/sync-cache.sqlite3"
DEFAULT_LIBRARY_CACHE_TTL = 0.0
DEFAULT_SPOTIFY_CONFIG = "auth/spotify.json"
DEFAULT_YT_BROWSER_AUTH_FILE = "auth/browser.json"
DEFAULT_YT_BROWSER_SESSION_DIR = "auth/ytmusic-browser-session"
YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_REQUIRED_COOKIE = "__Secure-3PAPISID"
DEFAULT_YT_BROWSER_LOGIN_TIMEOUT = 300.0
DEFAULT_HEARTBEAT_INTERVAL = 20.0
DEFAULT_HEARTBEAT_TIMEOUT = 15.0
SPOTIFY_RETRY_ATTEMPTS = 5
SPOTIFY_RETRY_BASE_DELAY = 2.0
SPOTIFY_MAX_RETRY_AFTER = 30.0
YTM_RETRY_ATTEMPTS = 4
YTM_RETRY_BASE_DELAY = 2.0
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|/|&| x | and | feat\.? | ft\.? | featuring )\s*", re.IGNORECASE)


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


def primary_search_artist(artists: Sequence[str]) -> str:
    for artist in artists:
        for part in ARTIST_SPLIT_RE.split(artist):
            cleaned = part.strip(" -")
            if cleaned:
                return cleaned
    return ""


def truncate_query(query: str, limit: int = 240) -> str:
    query = re.sub(r"\s+", " ", query).strip()
    if len(query) <= limit:
        return query
    truncated = query[:limit].rsplit(" ", 1)[0].strip()
    return truncated or query[:limit]


def build_spotify_search_queries(wanted: Track) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    title = wanted.title.strip()
    primary_artist = primary_search_artist(wanted.artists)
    candidates = [
        f"track:{title} artist:{primary_artist}" if title and primary_artist else "",
        f"track:{title}" if title else "",
        f"{title} {primary_artist}".strip(),
        title,
    ]
    for candidate in candidates:
        query = truncate_query(candidate)
        if query and query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


def is_spotify_query_too_long_error(exc: BaseException) -> bool:
    return "query exceeds maximum length" in str(exc).lower()


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


class CommandHeartbeat:
    def __init__(
        self,
        command: str | None,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL,
        timeout_seconds: float = DEFAULT_HEARTBEAT_TIMEOUT,
        run_fn: Callable[..., object] = subprocess.run,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.command = (command or "").strip()
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self._run_fn = run_fn
        self._time_fn = time_fn
        self._last_sent_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.command) and self.interval_seconds > 0

    def maybe_beat(self, *, force: bool = False) -> None:
        if not self.enabled:
            return
        now = self._time_fn()
        if not force and self._last_sent_at and (now - self._last_sent_at) < self.interval_seconds:
            return
        self._last_sent_at = now
        try:
            result = self._run_fn(
                self.command,
                shell=True,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            print(f"heartbeat command failed: {exc}", file=sys.stderr)
            return
        if getattr(result, "returncode", 1) != 0:
            detail = ((getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").strip().splitlines() or [""])[0]
            if detail:
                print(f"heartbeat command failed with exit {result.returncode}: {detail}", file=sys.stderr)
            else:
                print(f"heartbeat command failed with exit {result.returncode}", file=sys.stderr)


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


def _read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def default_yt_auth_file() -> str:
    explicit = os.environ.get("YTMUSIC_AUTH_FILE") or os.environ.get("YTMUSIC_OAUTH")
    if explicit:
        return explicit
    return DEFAULT_YT_BROWSER_AUTH_FILE


def load_spotify_config(path: Path | str | None = None) -> dict[str, str]:
    config_path = Path(path or os.environ.get("MUSIC_SYNC_SPOTIFY_CONFIG", DEFAULT_SPOTIFY_CONFIG)).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    data = _read_json_object(config_path)
    allowed = {"auth", "client_id", "client_secret", "redirect_uri", "cache"}
    return {key: str(value) for key, value in data.items() if key in allowed and value}


def _cookie_value(cookie_header: str, name: str) -> str | None:
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header.replace('"', ""))
    except Exception:
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def yt_sapisid_authorization(cookie_header: str, origin: str = YTMUSIC_ORIGIN, timestamp: int | None = None) -> str:
    sapisid = _cookie_value(cookie_header, YTMUSIC_REQUIRED_COOKIE)
    if not sapisid:
        raise RuntimeError(
            f"YouTube Music browser session is not logged in; missing {YTMUSIC_REQUIRED_COOKIE}. "
            "Complete Google/YouTube Music login in the opened browser tab, then rerun."
        )
    unix_timestamp = str(int(timestamp if timestamp is not None else time.time()))
    digest = sha1(f"{unix_timestamp} {sapisid} {origin}".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {unix_timestamp}_{digest}"


def build_yt_browser_auth_headers(
    cookie_header: str,
    *,
    user_agent: str,
    origin: str = YTMUSIC_ORIGIN,
    authuser: str = "0",
    timestamp: int | None = None,
) -> dict[str, str]:
    if not cookie_header.strip():
        raise RuntimeError("YouTube Music browser session has no cookies; login did not complete.")
    authorization = yt_sapisid_authorization(cookie_header, origin=origin, timestamp=timestamp)
    return {
        "accept": "*/*",
        "content-type": "application/json",
        "content-encoding": "gzip",
        "origin": origin,
        "x-origin": origin,
        "referer": f"{origin}/",
        "user-agent": user_agent,
        "x-goog-authuser": authuser,
        "cookie": cookie_header,
        "authorization": authorization,
    }


def _playwright_cookie_header(cookies: Sequence[dict]) -> str:
    # context.cookies([music.youtube.com]) already returns only cookies applicable to that URL.
    # Sort for deterministic auth/browser.json diffs and tests.
    pairs = []
    for cookie in sorted(cookies, key=lambda item: (str(item.get("name", "")), str(item.get("domain", "")))):
        name = str(cookie.get("name", "")).strip()
        value = str(cookie.get("value", ""))
        if name:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def ensure_yt_browser_auth_from_session(
    auth_path: Path,
    *,
    session_dir: Path,
    headless: bool = False,
    login_timeout_seconds: float = DEFAULT_YT_BROWSER_LOGIN_TIMEOUT,
) -> None:
    """Create/refresh ytmusicapi browser auth from a persistent real browser session.

    First run opens a Chromium window using session_dir as the user-data dir. The user logs in once.
    Later runs reuse that browser profile and refresh auth/browser.json from live cookies without pasted headers.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised by integration usage, not unit tests
        raise RuntimeError(
            "YouTube browser-session auth requires Playwright. Run: uv sync && uv run playwright install chromium"
        ) from exc

    auth_path.parent.mkdir(parents=True, exist_ok=True)
    session_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + login_timeout_seconds

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(session_dir),
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                user_agent = str(page.evaluate("navigator.userAgent"))
                cookie_header = _playwright_cookie_header(context.cookies([YTMUSIC_ORIGIN]))

                if not _cookie_value(cookie_header, YTMUSIC_REQUIRED_COOKIE):
                    page.goto(YTMUSIC_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
                    print(
                        "YouTube Music login required. Complete login in the opened browser window; "
                        "this browser profile will be reused on future runs.",
                        file=sys.stderr,
                    )
                while not _cookie_value(cookie_header, YTMUSIC_REQUIRED_COOKIE) and time.time() < deadline:
                    page.wait_for_timeout(2_000)
                    cookie_header = _playwright_cookie_header(context.cookies([YTMUSIC_ORIGIN]))
                    user_agent = str(page.evaluate("navigator.userAgent"))

                headers = build_yt_browser_auth_headers(cookie_header, user_agent=user_agent)
                auth_path.write_text(json.dumps(headers, ensure_ascii=True, indent=4, sort_keys=True) + "\n", encoding="utf-8")
                print(f"YouTube Music browser auth refreshed from persistent session: {auth_path}", file=sys.stderr)
            finally:
                context.close()
    except PlaywrightError as exc:  # pragma: no cover - depends on local browser installation/display
        raise RuntimeError(
            "Could not open the persistent YouTube Music browser session. "
            "If this is the first run, install the browser with: uv run playwright install chromium"
        ) from exc


def should_prepare_yt_browser_session(auth_mode: str, auth_path: Path, refresh: bool) -> bool:
    if refresh or auth_mode == "browser-session":
        return True
    return auth_mode == "auto" and auth_path.name == "browser.json" and not auth_path.exists()


def is_ytm_auth_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    signed_out = "sign in" in message and ("liked" in message or "tracks" in message or "operation" in message)
    unauthorized = "http 401" in message or "unauthorized" in message
    return signed_out or (unauthorized and "signed in" in message)


def ytm_auth_expired_message() -> str:
    return (
        "YouTube Music auth appears expired or signed out. Refresh it from the persistent browser session: "
        "uv run python src/music_liked_sync.py --yt-auth browser-session --yt-refresh-browser-auth. "
        "First run opens a real browser window; log in once and the session is reused after that."
    )


def retry_ytm_call(
    fn,
    *,
    label: str,
    attempts: int = YTM_RETRY_ATTEMPTS,
    base_delay: float = YTM_RETRY_BASE_DELAY,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except KeyError as exc:
            if is_ytm_auth_error(exc):
                raise RuntimeError(ytm_auth_expired_message()) from exc
            raise
        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            delay = base_delay * attempt
            print(
                f"{label}: transient non-JSON response from YouTube Music; retry {attempt}/{attempts - 1} in {delay:.1f}s",
                file=sys.stderr,
            )
            sleep_fn(delay)
        except Exception as exc:
            if is_ytm_auth_error(exc):
                raise RuntimeError(ytm_auth_expired_message()) from exc
            raise
    if last_exc:
        raise last_exc


def spotify_retry_delay_seconds(
    exc: BaseException,
    attempt: int,
    *,
    base_delay: float = SPOTIFY_RETRY_BASE_DELAY,
    max_retry_after: float = SPOTIFY_MAX_RETRY_AFTER,
) -> float:
    headers = getattr(exc, "headers", {}) or {}
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        try:
            parsed = float(retry_after)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed >= 0:
            return min(parsed, max_retry_after)
    return min(base_delay * attempt, max_retry_after)


def is_spotify_transient_error(exc: BaseException) -> bool:
    return getattr(exc, "http_status", None) in {429, 500, 502, 503, 504}


def retry_spotify_call(
    fn,
    *,
    label: str,
    attempts: int = SPOTIFY_RETRY_ATTEMPTS,
    base_delay: float = SPOTIFY_RETRY_BASE_DELAY,
    max_retry_after: float = SPOTIFY_MAX_RETRY_AFTER,
    sleep_fn: Callable[[float], None] = time.sleep,
):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            if is_spotify_query_too_long_error(exc):
                raise
            if not is_spotify_transient_error(exc):
                raise
            last_exc = exc
            if attempt >= attempts:
                raise
            delay = spotify_retry_delay_seconds(exc, attempt, base_delay=base_delay, max_retry_after=max_retry_after)
            print(
                f"{label}: transient Spotify error {getattr(exc, 'http_status', 'unknown')}; retry {attempt}/{attempts - 1} in {delay:.1f}s",
                file=sys.stderr,
            )
            sleep_fn(delay)
    if last_exc:
        raise last_exc


class SyncCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS matches (
                    direction TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_track_json TEXT NOT NULL,
                    target_track_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (direction, source_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS liked_tracks (
                    service TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (service, source_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS library_cache (
                    service TEXT NOT NULL PRIMARY KEY,
                    tracks_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _serialize_track(track: Track) -> str:
        return json.dumps(asdict(track), ensure_ascii=False)

    @staticmethod
    def _deserialize_track(payload: str) -> Track:
        data = json.loads(payload)
        return Track(
            title=str(data.get("title") or ""),
            artists=tuple(str(artist) for artist in (data.get("artists") or [])),
            source_id=str(data.get("source_id") or ""),
            duration_ms=data.get("duration_ms"),
            album=data.get("album"),
        )

    def store_match(self, direction: str, source: Track, target: Track) -> None:
        source_key = normalize_key(source.title, source.artists)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO matches(direction, source_key, source_track_json, target_track_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(direction, source_key) DO UPDATE SET
                    source_track_json=excluded.source_track_json,
                    target_track_json=excluded.target_track_json,
                    updated_at=excluded.updated_at
                """,
                (
                    direction,
                    source_key,
                    self._serialize_track(source),
                    self._serialize_track(target),
                    now,
                ),
            )
            conn.commit()

    def get_match(self, direction: str, source: Track) -> Track | None:
        source_key = normalize_key(source.title, source.artists)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT target_track_json FROM matches WHERE direction = ? AND source_key = ?",
                (direction, source_key),
            ).fetchone()
        if not row:
            return None
        try:
            return self._deserialize_track(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def mark_liked(self, service: str, source_id: str) -> None:
        self.mark_liked_many(service, [source_id])

    def mark_liked_many(self, service: str, source_ids: Sequence[str]) -> None:
        now = time.time()
        rows = [(service, source_id, now) for source_id in sorted(set(source_ids)) if source_id]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO liked_tracks(service, source_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service, source_id) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            conn.commit()

    def is_liked(self, service: str, source_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM liked_tracks WHERE service = ? AND source_id = ? LIMIT 1",
                (service, source_id),
            ).fetchone()
        return row is not None

    def store_library(self, service: str, tracks: Sequence[Track]) -> None:
        payload = json.dumps([asdict(track) for track in tracks], ensure_ascii=False)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO library_cache(service, tracks_json, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    tracks_json=excluded.tracks_json,
                    fetched_at=excluded.fetched_at
                """,
                (service, payload, now),
            )
            conn.commit()

    def get_library(self, service: str, max_age_seconds: float) -> list[Track] | None:
        if max_age_seconds <= 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tracks_json, fetched_at FROM library_cache WHERE service = ?",
                (service,),
            ).fetchone()
        if not row:
            return None
        tracks_json, fetched_at = row
        if (time.time() - float(fetched_at)) > max_age_seconds:
            return None
        try:
            raw_tracks = json.loads(tracks_json)
            if not isinstance(raw_tracks, list):
                return None
            return [self._deserialize_track(json.dumps(item, ensure_ascii=False)) for item in raw_tracks]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None


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
        heartbeat: CommandHeartbeat | None = None,
    ) -> None:
        self.market = market
        self.heartbeat = heartbeat
        self.mode = self._resolve_auth_mode(auth_mode, client_id, client_secret)

        from spotipy import Spotify
        from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

        auth_manager = (
            SpotifyPKCE(
                client_id=client_id or os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID"),
                redirect_uri=redirect_uri
                or os.environ.get("SPOTIPY_REDIRECT_URI")
                or os.environ.get("SPOTIFY_REDIRECT_URI")
                or "http://127.0.0.1:8888/callback",
                scope=SPOTIFY_SCOPES,
                cache_path=cache_path or os.environ.get("SPOTIFY_TOKEN_CACHE") or ".cache-spotify",
                open_browser=True,
            )
            if self.mode == "pkce"
            else SpotifyOAuth(
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
        self.client = Spotify(auth_manager=auth_manager, retries=0, status_retries=0, backoff_factor=0)

    @staticmethod
    def _resolve_auth_mode(auth_mode: str, client_id: str | None, client_secret: str | None) -> str:
        if auth_mode not in {"auto", "oauth", "pkce"}:
            raise ValueError(f"unsupported Spotify auth backend: {auth_mode}")
        if auth_mode != "auto":
            return auth_mode
        has_spotify_client_id = bool(client_id or os.environ.get("SPOTIPY_CLIENT_ID") or os.environ.get("SPOTIFY_CLIENT_ID"))
        has_spotify_client_secret = bool(
            client_secret or os.environ.get("SPOTIPY_CLIENT_SECRET") or os.environ.get("SPOTIFY_CLIENT_SECRET")
        )
        if has_spotify_client_id and has_spotify_client_secret:
            return "oauth"
        if has_spotify_client_id:
            return "pkce"
        return "oauth"

    def liked_tracks(self) -> list[Track]:
        tracks: list[Track] = []
        offset = 0
        while True:
            heartbeat = getattr(self, "heartbeat", None)
            if heartbeat:
                heartbeat.maybe_beat()
            page = retry_spotify_call(
                lambda offset=offset: self.client.current_user_saved_tracks(limit=50, offset=offset, market=self.market),
                label="Spotify current_user_saved_tracks",
            )
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
        for query in build_spotify_search_queries(wanted):
            heartbeat = getattr(self, "heartbeat", None)
            if heartbeat:
                heartbeat.maybe_beat()
            try:
                page = retry_spotify_call(
                    lambda query=query: self.client.search(q=query, type="track", limit=limit, market=self.market),
                    label="Spotify search",
                )
            except Exception as exc:
                if is_spotify_query_too_long_error(exc):
                    continue
                raise
            items = ((page.get("tracks") or {}).get("items") or [])
            tracks = [t for t in (parse_spotify_track(item) for item in items) if t]
            if tracks:
                return tracks
        return []

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
            heartbeat = getattr(self, "heartbeat", None)
            if heartbeat:
                heartbeat.maybe_beat()
            retry_spotify_call(
                lambda chunk=chunk: self.client.current_user_saved_tracks_add(tracks=chunk),
                label="Spotify current_user_saved_tracks_add",
            )
            sleep_between_batches(index, len(chunks), batch_delay, sleep_fn)


class YTMusicBackend:
    def __init__(
        self,
        auth_path: Path,
        client_id: str | None,
        client_secret: str | None,
        auth_mode: str = "auto",
        heartbeat: CommandHeartbeat | None = None,
    ) -> None:
        from ytmusicapi import YTMusic
        from ytmusicapi.auth.oauth import OAuthCredentials

        if not auth_path.exists():
            raise FileNotFoundError(f"YouTube Music auth file missing: {auth_path}")

        self.heartbeat = heartbeat
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
        heartbeat = getattr(self, "heartbeat", None)
        if heartbeat:
            heartbeat.maybe_beat(force=True)
        result = retry_ytm_call(
            lambda: self.client.get_liked_songs(limit=limit or 10000),
            label="YTM get_liked_songs",
        )
        items = result.get("tracks", []) if isinstance(result, dict) else result
        return [t for t in (parse_ytm_track(item) for item in (items or [])) if t]

    def search_track(self, wanted: Track, limit: int = 5) -> list[Track]:
        query = f"{wanted.title} {' '.join(wanted.artists)}".strip()
        heartbeat = getattr(self, "heartbeat", None)
        if heartbeat:
            heartbeat.maybe_beat()
        items = retry_ytm_call(
            lambda: self.client.search(query, filter="songs", limit=limit) or [],
            label="YTM search",
        )
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
                heartbeat = getattr(self, "heartbeat", None)
                if heartbeat:
                    heartbeat.maybe_beat()
                retry_ytm_call(
                    lambda track_id=track.source_id: self.client.rate_song(track_id, "LIKE"),
                    label=f"YTM rate_song {track.source_id}",
                )
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
    cache: SyncCache | None = None,
    cache_direction: str | None = None,
    cache_read: bool = True,
    cache_write: bool = True,
    heartbeat: CommandHeartbeat | None = None,
) -> tuple[list[tuple[Track, Track]], list[Track]]:
    matched: list[tuple[Track, Track]] = []
    unmatched: list[Track] = []
    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    chunks = batched(candidates_to_process, batch_size)
    for batch_index, chunk in enumerate(chunks):
        for wanted in chunk:
            if heartbeat:
                heartbeat.maybe_beat()
            cached_match = None
            if cache and cache_direction and cache_read:
                cached_match = cache.get_match(cache_direction, wanted)
            if cached_match:
                matched.append((wanted, cached_match))
                print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
                continue
            try:
                candidates = search_fn(wanted)
            except RuntimeError:
                raise
            except Exception as exc:
                summary = str(exc).strip().splitlines()[0][:180] if str(exc).strip() else exc.__class__.__name__
                print(f"{label}: search failed for {wanted.display}; treating as unresolved ({summary})", file=sys.stderr)
                unmatched.append(wanted)
                print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
                continue
            match = best_match(wanted, candidates)
            if match:
                matched.append((wanted, match))
                if cache and cache_direction and cache_write:
                    cache.store_match(cache_direction, wanted, match)
            else:
                unmatched.append(wanted)
            print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
        sleep_between_batches(batch_index, len(chunks), batch_delay, sleep_fn)
    print("".ljust(90), end="\r")
    return matched, unmatched


def build_arg_parser() -> argparse.ArgumentParser:
    spotify_config = load_spotify_config()
    parser = argparse.ArgumentParser(description="Sync Spotify and YouTube Music liked songs")
    parser.add_argument("--yt-auth", choices=("auto", "oauth", "browser", "browser-session"), default=os.environ.get("YTMUSIC_AUTH", "browser-session"), help="YouTube Music auth type; browser-session opens/reuses a persistent browser profile and writes browser headers JSON")
    parser.add_argument("--yt-auth-file", "--oauth", dest="yt_auth_file", default=default_yt_auth_file(), help="YouTube Music auth JSON path; --oauth is kept as a backwards-compatible alias")
    parser.add_argument("--yt-client-id", default=os.environ.get("YTMUSIC_CLIENT_ID"))
    parser.add_argument("--yt-browser-session-dir", default=os.environ.get("YTMUSIC_BROWSER_SESSION_DIR", DEFAULT_YT_BROWSER_SESSION_DIR), help="persistent browser profile used for YouTube Music login/session reuse")
    parser.add_argument("--yt-browser-headless", action="store_true", help="run the YouTube Music browser session headless after first login is complete")
    parser.add_argument("--yt-browser-login-timeout", type=non_negative_float, default=float(os.environ.get("YTMUSIC_BROWSER_LOGIN_TIMEOUT", DEFAULT_YT_BROWSER_LOGIN_TIMEOUT)), help="seconds to wait for first-time YouTube Music browser login")
    parser.add_argument("--yt-refresh-browser-auth", action="store_true", help="refresh auth/browser.json from the persistent browser session before syncing")
    parser.add_argument("--yt-client-secret", default=os.environ.get("YTMUSIC_CLIENT_SECRET"))
    parser.add_argument("--spotify-auth", choices=("auto", "oauth", "pkce"), default=os.environ.get("SPOTIFY_AUTH") or spotify_config.get("auth", "auto"), help="Spotify auth backend: oauth uses client secret, pkce uses client ID only")
    parser.add_argument("--spotify-client-id", default=os.environ.get("SPOTIFY_CLIENT_ID") or os.environ.get("SPOTIPY_CLIENT_ID") or spotify_config.get("client_id"))
    parser.add_argument("--spotify-client-secret", default=os.environ.get("SPOTIFY_CLIENT_SECRET") or os.environ.get("SPOTIPY_CLIENT_SECRET") or spotify_config.get("client_secret"))
    parser.add_argument("--spotify-redirect-uri", default=os.environ.get("SPOTIFY_REDIRECT_URI") or os.environ.get("SPOTIPY_REDIRECT_URI") or spotify_config.get("redirect_uri") or "http://127.0.0.1:8888/callback")
    parser.add_argument("--spotify-cache", default=os.environ.get("SPOTIFY_TOKEN_CACHE") or spotify_config.get("cache", ".cache-spotify"), help="Spotipy OAuth token cache path")
    parser.add_argument("--market", default=os.environ.get("MUSIC_SYNC_MARKET", DEFAULT_MARKET), help="Spotify market code used for search/library reads")
    parser.add_argument("--apply", action="store_true", help="actually save/like matched tracks; default is dry-run")
    parser.add_argument("--max-add", type=positive_int, default=None, help="optional cap on tracks to add per direction; default processes all missing tracks")
    parser.add_argument("--batch-size", type=positive_int, default=int(os.environ.get("MUSIC_SYNC_BATCH_SIZE", DEFAULT_BATCH_SIZE)), help="tracks to search/save before pausing; Spotify writes are capped to 50 by API")
    parser.add_argument("--batch-delay", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_BATCH_DELAY", DEFAULT_BATCH_DELAY)), help="seconds to sleep between batches")
    parser.add_argument("--cache-db", default=os.environ.get("MUSIC_SYNC_CACHE_DB", DEFAULT_CACHE_DB), help="sqlite path for persistent sync cache")
    parser.add_argument("--cache-library-ttl", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_LIBRARY_CACHE_TTL", DEFAULT_LIBRARY_CACHE_TTL)), help="seconds to reuse cached liked libraries; 0 disables library reuse")
    parser.add_argument("--no-cache-read", dest="cache_read", action="store_false", help="disable reading cached matches/library")
    parser.add_argument("--no-cache-write", dest="cache_write", action="store_false", help="disable writing cache")
    parser.add_argument("--heartbeat-command", default=os.environ.get("MUSIC_SYNC_HEARTBEAT_COMMAND"), help="optional shell command to keep the YouTube Music session warm during long runs")
    parser.add_argument("--heartbeat-interval", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_HEARTBEAT_INTERVAL", DEFAULT_HEARTBEAT_INTERVAL)), help="minimum seconds between heartbeat command runs; 0 disables heartbeat")
    parser.add_argument("--heartbeat-timeout", type=non_negative_float, default=float(os.environ.get("MUSIC_SYNC_HEARTBEAT_TIMEOUT", DEFAULT_HEARTBEAT_TIMEOUT)), help="seconds to wait for each heartbeat command")
    parser.add_argument("--spotify-to-ytm", action="store_true", help="only sync Spotify liked songs into YouTube Music")
    parser.add_argument("--ytm-to-spotify", action="store_true", help="only sync YouTube Music liked songs into Spotify")
    parser.add_argument("--report", default="sync-report.json", help="write JSON report here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    auth_path = Path(args.yt_auth_file).expanduser()
    if not auth_path.is_absolute():
        auth_path = Path.cwd() / auth_path
    yt_browser_session_dir = Path(args.yt_browser_session_dir).expanduser()
    if not yt_browser_session_dir.is_absolute():
        yt_browser_session_dir = Path.cwd() / yt_browser_session_dir

    if should_prepare_yt_browser_session(args.yt_auth, auth_path, args.yt_refresh_browser_auth):
        try:
            ensure_yt_browser_auth_from_session(
                auth_path,
                session_dir=yt_browser_session_dir,
                headless=args.yt_browser_headless,
                login_timeout_seconds=args.yt_browser_login_timeout,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    ytm_backend_auth_mode = "browser" if args.yt_auth == "browser-session" else args.yt_auth

    heartbeat = CommandHeartbeat(
        args.heartbeat_command,
        interval_seconds=args.heartbeat_interval,
        timeout_seconds=args.heartbeat_timeout,
    )

    spotify = SpotifyBackend(
        auth_mode=args.spotify_auth,
        client_id=args.spotify_client_id,
        client_secret=args.spotify_client_secret,
        redirect_uri=args.spotify_redirect_uri,
        cache_path=args.spotify_cache,
        market=args.market,
        heartbeat=heartbeat,
    )
    cache_path = Path(args.cache_db).expanduser()
    if not cache_path.is_absolute():
        cache_path = Path.cwd() / cache_path
    cache = SyncCache(cache_path)
    try:
        ytm = YTMusicBackend(auth_path, args.yt_client_id, args.yt_client_secret, auth_mode=ytm_backend_auth_mode, heartbeat=heartbeat)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print(
            "Run one of: uv run ytmusicapi oauth --file auth/oauth.json "
            "--client-id '<CLIENT_ID>' --client-secret '<CLIENT_SECRET>'; "
            "or uv run ytmusicapi browser --file auth/browser.json",
            file=sys.stderr,
        )
        return 2

    spotify_liked = cache.get_library("spotify", args.cache_library_ttl) if args.cache_read else None
    if spotify_liked is None:
        spotify_liked = spotify.liked_tracks()
        if args.cache_write:
            cache.store_library("spotify", spotify_liked)

    ytm_liked = cache.get_library("ytm", args.cache_library_ttl) if args.cache_read else None
    if ytm_liked is None:
        try:
            ytm_liked = ytm.liked_tracks()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.cache_write:
            cache.store_library("ytm", ytm_liked)
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
        "yt_auth_requested": args.yt_auth,
        "yt_browser_session_dir": str(yt_browser_session_dir),
        "cache_db": str(cache_path),
        "cache_read": bool(args.cache_read),
        "cache_write": bool(args.cache_write),
        "cache_library_ttl": args.cache_library_ttl,
        "heartbeat_enabled": heartbeat.enabled,
        "heartbeat_interval": args.heartbeat_interval,
        "heartbeat_timeout": args.heartbeat_timeout,
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
            cache=cache,
            cache_direction="spotify_to_ytm",
            cache_read=args.cache_read,
            cache_write=args.cache_write,
            heartbeat=heartbeat,
        )
        if args.apply:
            to_like = [match for _, match in matched]
            if args.cache_read:
                to_like = [track for track in to_like if not cache.is_liked("ytm", track.source_id)]
            try:
                ytm.like_tracks(
                    to_like,
                    batch_size=args.batch_size,
                    batch_delay=args.batch_delay,
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
        missing = compute_missing(ytm_liked, spotify_liked)
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
            heartbeat=heartbeat,
        )
        if args.apply:
            to_save = [match for _, match in matched]
            if args.cache_read:
                to_save = [track for track in to_save if not cache.is_liked("spotify", track.source_id)]
            spotify.save_tracks(
                to_save,
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
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
        "yt_auth_requested": args.yt_auth,
        "yt_browser_session_dir": str(yt_browser_session_dir),
        "cache_db": str(cache_path),
        "cache_read": bool(args.cache_read),
        "cache_write": bool(args.cache_write),
        "cache_library_ttl": args.cache_library_ttl,
        "heartbeat_enabled": heartbeat.enabled,
        "heartbeat_interval": args.heartbeat_interval,
        "heartbeat_timeout": args.heartbeat_timeout,
        "spotify_to_ytm": report["spotify_to_ytm"],
        "ytm_to_spotify": report["ytm_to_spotify"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
