import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from pathlib import Path

from .browser_auth import BrowserSessionConfig, ensure_browser_session
from .constants import (
    DEFAULT_YT_BROWSER_LOGIN_TIMEOUT,
    YTMUSIC_ORIGIN,
    YTMUSIC_REQUIRED_COOKIE,
    YTM_RETRY_ATTEMPTS,
    YTM_RETRY_BASE_DELAY,
    DEFAULT_BATCH_DELAY,
    DEFAULT_BATCH_SIZE,
)
from .models import FatalSearchError, Track
from .utils import (
    batched,
    cookie_value,
    sleep_between_batches,
)



def yt_sapisid_authorization(cookie_header: str, origin: str = YTMUSIC_ORIGIN, timestamp: int | None = None) -> str:
    sapisid = cookie_value(cookie_header, YTMUSIC_REQUIRED_COOKIE)
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




def ensure_yt_browser_auth_from_session(
    *,
    session_dir: Path,
    headless: bool = False,
    login_timeout_seconds: float = DEFAULT_YT_BROWSER_LOGIN_TIMEOUT,
    lock_path: Path | str,
) -> dict[str, str]:
    lock_path = Path(lock_path).expanduser()
    if not lock_path.is_absolute():
        lock_path = Path.cwd() / lock_path
    config = BrowserSessionConfig(
        session_dir=session_dir,
        origin=YTMUSIC_ORIGIN,
        required_cookie=YTMUSIC_REQUIRED_COOKIE,
        lock_path=lock_path,
        label="YouTube Music",
        headless=headless,
        login_timeout_seconds=login_timeout_seconds,
    )
    return ensure_browser_session(
        config,
        lambda page, cookie_header, user_agent: build_yt_browser_auth_headers(cookie_header, user_agent=user_agent),
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
    return Track(
        title=title,
        artists=tuple(artists),
        source_id=video_id,
        duration_ms=duration_ms,
        album=(item.get("album") or {}).get("name") if isinstance(item.get("album"), dict) else None
    )


def is_ytm_auth_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    signed_out = "sign in" in message and ("liked" in message or "tracks" in message or "operation" in message)
    unauthorized = "http 401" in message or "unauthorized" in message
    return signed_out or (unauthorized and "signed in" in message)


def ytm_auth_expired_message() -> str:
    return (
        "YouTube Music auth appears expired or signed out. Rerun the sync so browser-session auth "
        "can refresh from the persistent browser session. If prompted, complete login in the opened browser window."
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
                raise FatalSearchError(ytm_auth_expired_message()) from exc
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
                raise FatalSearchError(ytm_auth_expired_message()) from exc
            raise
    if last_exc:
        raise last_exc


class YTMusicBackend:
    def __init__(
        self,
        auth_headers: dict[str, str],
    ) -> None:
        from ytmusicapi import YTMusic

        if not auth_headers:
            raise ValueError("YouTube Music browser-session auth headers are missing")

        self.mode = "browser-session"
        self.client = YTMusic(auth_headers)

    def liked_tracks(self, limit: int | None = None, verbose: bool = False) -> list[Track]:
        if verbose:
            print("Fetching YouTube Music liked songs...", file=sys.stderr)
        result = retry_ytm_call(
            lambda: self.client.get_liked_songs(limit=limit or 10000),
            label="YTM get_liked_songs",
        )
        items = result.get("tracks", []) if isinstance(result, dict) else result
        tracks = [t for t in (parse_ytm_track(item) for item in (items or [])) if t]
        if verbose:
            print(f"  Finished fetching {len(tracks)} tracks from YouTube Music", file=sys.stderr)
        return tracks

    def search_track(self, wanted: Track, limit: int = 5) -> list[Track]:
        query = f"{wanted.title} {' '.join(wanted.artists)}".strip()
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
        max_workers: int = 4,
        verbose: bool = False,
    ) -> None:
        if verbose:
            print(f"Liking {len(tracks)} tracks on YouTube Music...", file=sys.stderr)
        chunks = batched(tracks, batch_size)
        print_lock = threading.Lock()

        def vprint_track(track_id):
            if verbose:
                with print_lock:
                    print(f"  [LIKE] {track_id}", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for index, chunk in enumerate(chunks):
                if verbose:
                    print(f"  Batch {index+1}/{len(chunks)} ({len(chunk)} tracks)", file=sys.stderr)
                futures = []
                for track in chunk:
                    def make_call(t=track):
                        vprint_track(t.source_id)
                        return retry_ytm_call(
                            lambda: self.client.rate_song(t.source_id, "LIKE"),
                            label=f"YTM rate_song {t.source_id}",
                        )
                    futures.append(executor.submit(make_call))
                
                for future in futures:
                    future.result()
                sleep_between_batches(index, len(chunks), batch_delay, sleep_fn)
