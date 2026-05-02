import json
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._spotify_client import (
    SpotifyAPIError as SpotifyAPIError,
    SpotifyWebClient as SpotifyWebClient,
    _is_persisted_query_error as _is_persisted_query_error,
    is_spotify_query_too_long_error as is_spotify_query_too_long_error,
    is_spotify_transient_error as is_spotify_transient_error,
    load_hash_cache as load_hash_cache,
    parse_spotify_track as parse_spotify_track,
    retry_spotify_call as retry_spotify_call,
    save_hash_cache as save_hash_cache,
    spotify_graphql_library_item_to_api_item as spotify_graphql_library_item_to_api_item,
    spotify_graphql_track_to_api_track as spotify_graphql_track_to_api_track,
    spotify_pathfinder_request_json as spotify_pathfinder_request_json,
    spotify_retry_delay_seconds as spotify_retry_delay_seconds,
)
from .browser_auth import BrowserSessionConfig, ensure_browser_session
from .constants import (
    DEFAULT_SPOTIFY_HASH_CACHE,
    DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT,
    SPOTIFY_WEB_ORIGIN,
    SPOTIFY_WEB_PATHFINDER_URL,
    SPOTIFY_WEB_REQUIRED_COOKIE,
    SPOTIFY_WEB_TOKEN_URL_PREFIX,
    DEFAULT_BATCH_DELAY,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MARKET,
)
from .models import FatalSearchError, SpotifyWebSessionState, Track
from .utils import (
    batched,
    primary_search_artist,
    safe_page_user_agent,
    sleep_between_batches,
    truncate_query,
)


def spotify_web_token_from_payload(data: dict) -> str:
    token = data.get("accessToken") or data.get("access_token")
    if not token:
        raise RuntimeError("Spotify Web Player token response did not include accessToken")
    if data.get("isAnonymous") is True:
        raise RuntimeError("Spotify Web Player returned an anonymous token; complete Spotify login, then rerun.")
    return str(token)


def wait_for_spotify_web_session_state(
    page, *, timeout_ms: int = 60_000, hash_cache_path: str | Path | None = None
) -> SpotifyWebSessionState:
    captured_headers: dict[str, str] = {}
    discovered_hashes: dict[str, str] = {}

    def is_token_response(response) -> bool:
        return response.url.startswith(SPOTIFY_WEB_TOKEN_URL_PREFIX) and response.status == 200

    def capture_pathfinder_request(request) -> None:
        if request.url != SPOTIFY_WEB_PATHFINDER_URL:
            return
        headers = request.headers
        for key in ("client-token", "spotify-app-version"):
            value = headers.get(key)
            if value:
                captured_headers[key] = value
        try:
            body = json.loads(request.post_data or "{}")
        except (json.JSONDecodeError, TypeError):
            return
        if isinstance(body, dict):
            op_name = body.get("operationName")
            sha = (body.get("extensions") or {}).get("persistedQuery", {}).get("sha256Hash")
            if op_name and sha:
                discovered_hashes[op_name] = sha

    page.on("request", capture_pathfinder_request)
    try:
        with page.expect_response(is_token_response, timeout=timeout_ms) as response_info:
            page.goto(SPOTIFY_WEB_ORIGIN, wait_until="domcontentloaded", timeout=timeout_ms)
        data = response_info.value.json()
        if not captured_headers.get("client-token"):
            try:
                request = page.wait_for_request(
                    lambda req: req.url == SPOTIFY_WEB_PATHFINDER_URL and bool(req.headers.get("client-token")),
                    timeout=5_000,
                )
                capture_pathfinder_request(request)
            except Exception:
                pass
    except Exception as exc:
        raise RuntimeError("Spotify Web Player token request did not complete from the loaded web app") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Spotify Web Player token response was not a JSON object")

    # Navigate to pages that trigger our target operations so we can capture their hashes.
    _target_operations = {"fetchLibraryTracks", "searchTopResultsList", "addToLibrary"}
    _discovery_navs = [
        (f"{SPOTIFY_WEB_ORIGIN}/collection/tracks", "fetchLibraryTracks"),
        (f"{SPOTIFY_WEB_ORIGIN}/search/discover-hash", "searchTopResultsList"),
    ]
    for url, _ in _discovery_navs:
        if _target_operations.issubset(discovered_hashes.keys()):
            break
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(2_000)
        except Exception:
            pass

    if hash_cache_path and discovered_hashes:
        save_hash_cache(discovered_hashes, hash_cache_path)

    return SpotifyWebSessionState(
        access_token=spotify_web_token_from_payload(data),
        user_agent=safe_page_user_agent(page),
        client_token=captured_headers.get("client-token"),
        app_version=captured_headers.get("spotify-app-version"),
    )


def ensure_spotify_web_session_state_from_session(
    session_dir: Path,
    *,
    headless: bool = False,
    login_timeout_seconds: float = DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT,
    lock_path: Path | str,
    hash_cache_path: str | Path = DEFAULT_SPOTIFY_HASH_CACHE,
) -> SpotifyWebSessionState:
    lock_path = Path(lock_path).expanduser()
    if not lock_path.is_absolute():
        lock_path = Path.cwd() / lock_path

    def _extract_state(page, cookie_header, user_agent):
        state = wait_for_spotify_web_session_state(page, hash_cache_path=hash_cache_path)
        if not state.user_agent:
            state = SpotifyWebSessionState(
                access_token=state.access_token,
                user_agent=user_agent,
                client_token=state.client_token,
                app_version=state.app_version,
            )
        return state

    config = BrowserSessionConfig(
        session_dir=session_dir,
        origin=SPOTIFY_WEB_ORIGIN,
        required_cookie=SPOTIFY_WEB_REQUIRED_COOKIE,
        lock_path=lock_path,
        label="Spotify Web Player",
        headless=headless,
        login_timeout_seconds=login_timeout_seconds,
    )
    return ensure_browser_session(config, _extract_state)


def build_spotify_search_queries(wanted: Track) -> list[str]:
    seen: set[str] = set()
    queries: list[str] = []
    from .utils import normalize_text, normalize_artist

    title = wanted.title.strip()
    norm_title = normalize_text(title, wanted.artists)
    primary_artist = primary_search_artist(wanted.artists)
    all_artists = " ".join(normalize_artist(a) for a in wanted.artists if a)

    candidates = [
        f"track:{title} artist:{primary_artist}" if title and primary_artist else "",
        f"track:{norm_title} artist:{primary_artist}" if norm_title and primary_artist else "",
        f"track:{title}" if title else "",
        f"track:{norm_title}" if norm_title else "",
        f"{title} {primary_artist}".strip(),
        f"{norm_title} {primary_artist}".strip(),
        f"{norm_title} {all_artists}".strip(),
        title,
        norm_title,
    ]
    for candidate in candidates:
        query = truncate_query(candidate)
        if query and query not in seen:
            seen.add(query)
            queries.append(query)
    return queries


class SpotifyBackend:
    def __init__(
        self,
        *,
        market: str = DEFAULT_MARKET,
        web_session_dir: Path | str,
        web_headless: bool = False,
        web_login_timeout_seconds: float = DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT,
        lock_path: Path | str,
        hash_cache_path: str | Path = DEFAULT_SPOTIFY_HASH_CACHE,
    ) -> None:
        self.market = market
        self.mode = "web-session"

        session_dir = Path(web_session_dir).expanduser()
        if not session_dir.is_absolute():
            session_dir = Path.cwd() / session_dir
        self.web_session_dir = session_dir
        self.client = SpotifyWebClient(
            lambda: ensure_spotify_web_session_state_from_session(
                session_dir,
                headless=web_headless,
                login_timeout_seconds=web_login_timeout_seconds,
                lock_path=lock_path,
                hash_cache_path=hash_cache_path,
            ),
            hash_cache_path=hash_cache_path,
        )

    def liked_tracks(self, max_workers: int = 4, verbose: bool = False) -> list[Track]:
        if verbose:
            print("Fetching Spotify liked tracks library...", file=sys.stderr)
        first_page = retry_spotify_call(
            lambda: self.client.current_user_saved_tracks(limit=50, offset=0, market=self.market),
            label="Spotify current_user_saved_tracks (page 1)",
        )
        total = int(first_page.get("total") or 0)
        if verbose:
            print(f"  Found {total} tracks in Spotify library", file=sys.stderr)
        items = first_page.get("items", []) or []
        tracks: list[Track] = []

        def parse_items(items):
            parsed_batch = []
            for item in items:
                parsed = parse_spotify_track(item)
                if parsed:
                    parsed_batch.append(parsed)
            return parsed_batch

        tracks.extend(parse_items(items))

        if total <= 50:
            return tracks

        offsets = list(range(50, total, 50))
        if verbose:
            print(f"  Parallel fetching {len(offsets)} more pages...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    retry_spotify_call,
                    lambda offset=offset: self.client.current_user_saved_tracks(limit=50, offset=offset, market=self.market),
                    label=f"Spotify current_user_saved_tracks (offset {offset})",
                )
                for offset in offsets
            ]
            for future in futures:
                page = future.result()
                tracks.extend(parse_items(page.get("items", []) or []))

        if verbose:
            print(f"  Finished fetching {len(tracks)} tracks from Spotify", file=sys.stderr)
        return tracks

    def search_track(self, wanted: Track, limit: int = 5) -> list[Track]:
        for query in build_spotify_search_queries(wanted):
            try:
                page = retry_spotify_call(
                    lambda query=query: self.client.search(q=query, type="track", limit=limit, market=self.market),
                    label="Spotify search",
                )
            except SpotifyAPIError as exc:
                if _is_persisted_query_error(exc):
                    raise FatalSearchError(str(exc)) from exc
                raise
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
        max_workers: int = 4,
        verbose: bool = False,
    ) -> None:
        if verbose:
            print(f"Saving {len(tracks)} tracks to Spotify...", file=sys.stderr)
        ids = [track.source_id.split(":")[-1] for track in tracks]
        effective_batch_size = min(batch_size, 50)
        chunks = batched(ids, effective_batch_size)
        for index, chunk in enumerate(chunks):
            if verbose:
                print(f"  [SAVE] Batch {index+1}/{len(chunks)} ({len(chunk)} tracks)", file=sys.stderr)
            retry_spotify_call(
                lambda chunk=chunk: self.client.current_user_saved_tracks_add(tracks=chunk),
                label="Spotify current_user_saved_tracks_add",
            )
            sleep_between_batches(index, len(chunks), batch_delay, sleep_fn)
