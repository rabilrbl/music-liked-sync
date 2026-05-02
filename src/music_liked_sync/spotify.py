import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .constants import (
    DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT,
    SPOTIFY_MAX_RETRY_AFTER,
    SPOTIFY_RETRY_ATTEMPTS,
    SPOTIFY_RETRY_BASE_DELAY,
    SPOTIFY_WEB_ORIGIN,
    SPOTIFY_WEB_PATHFINDER_URL,
    SPOTIFY_WEB_REQUIRED_COOKIE,
    SPOTIFY_WEB_TOKEN_URL_PREFIX,
    DEFAULT_BATCH_DELAY,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MARKET,
)
from .models import SpotifyWebSessionState, Track
from .utils import (
    batched,
    browser_session_lock,
    cookie_value,
    playwright_cookie_header,
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


def wait_for_spotify_web_session_state(page, *, timeout_ms: int = 60_000) -> SpotifyWebSessionState:
    captured_headers: dict[str, str] = {}

    def is_token_response(response) -> bool:
        return response.url.startswith(SPOTIFY_WEB_TOKEN_URL_PREFIX) and response.status == 200

    def capture_pathfinder_headers(request) -> None:
        if request.url != SPOTIFY_WEB_PATHFINDER_URL:
            return
        headers = request.headers
        for key in ("client-token", "spotify-app-version"):
            value = headers.get(key)
            if value:
                captured_headers[key] = value

    page.on("request", capture_pathfinder_headers)
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
                capture_pathfinder_headers(request)
            except Exception:
                pass
    except Exception as exc:
        raise RuntimeError("Spotify Web Player token request did not complete from the loaded web app") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Spotify Web Player token response was not a JSON object")
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
) -> SpotifyWebSessionState:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Spotify web-session auth requires Playwright. Run: uv sync && uv run playwright install chromium"
        ) from exc

    session_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(lock_path).expanduser()
    if not lock_path.is_absolute():
        lock_path = Path.cwd() / lock_path
    deadline = time.time() + login_timeout_seconds

    try:
        with browser_session_lock(lock_path):
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    str(session_dir),
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    user_agent = safe_page_user_agent(page)
                    cookie_header = playwright_cookie_header(context.cookies([SPOTIFY_WEB_ORIGIN]))

                    if not cookie_value(cookie_header, SPOTIFY_WEB_REQUIRED_COOKIE):
                        page.goto(SPOTIFY_WEB_ORIGIN, wait_until="domcontentloaded", timeout=60_000)
                        user_agent = safe_page_user_agent(page)
                        print(
                            "Spotify Web Player login required. Complete login in the opened browser window; "
                            "this browser profile will be reused on future runs.",
                            file=sys.stderr,
                        )
                    while not cookie_value(cookie_header, SPOTIFY_WEB_REQUIRED_COOKIE) and time.time() < deadline:
                        page.wait_for_timeout(2_000)
                        cookie_header = playwright_cookie_header(context.cookies([SPOTIFY_WEB_ORIGIN]))
                        user_agent = safe_page_user_agent(page)

                    if not cookie_value(cookie_header, SPOTIFY_WEB_REQUIRED_COOKIE):
                        raise RuntimeError(
                            f"Spotify Web Player session is not logged in; missing {SPOTIFY_WEB_REQUIRED_COOKIE}. "
                            "Complete Spotify login in the opened browser window, then rerun."
                        )

                    state = wait_for_spotify_web_session_state(page)
                    if not state.user_agent:
                        state = SpotifyWebSessionState(
                            access_token=state.access_token,
                            user_agent=user_agent,
                            client_token=state.client_token,
                            app_version=state.app_version,
                        )
                    print("Spotify Web Player token refreshed from persistent browser session", file=sys.stderr)
                    return state
                finally:
                    context.close()
    except PlaywrightError as exc:
        raise RuntimeError(
            "Could not open the persistent Spotify Web Player browser session. "
            "If this is the first run, install the browser with: uv run playwright install chromium"
        ) from exc


class SpotifyAPIError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.headers = headers or {}


def spotify_pathfinder_request_json(payload: dict, *, state: SpotifyWebSessionState):
    headers = {
        "accept": "application/json",
        "app-platform": "WebPlayer",
        "authorization": f"Bearer {state.access_token}",
        "content-type": "application/json;charset=UTF-8",
        "origin": SPOTIFY_WEB_ORIGIN,
        "referer": f"{SPOTIFY_WEB_ORIGIN}/",
        "user-agent": state.user_agent,
    }
    if state.client_token:
        headers["client-token"] = state.client_token
    if state.app_version:
        headers["spotify-app-version"] = state.app_version
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(SPOTIFY_WEB_PATHFINDER_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SpotifyAPIError(
            f"Spotify Web Player pathfinder HTTP {exc.code}: {body}",
            http_status=exc.code,
            headers=dict(exc.headers.items()),
        ) from exc
    parsed = json.loads(raw.decode("utf-8")) if raw else {}
    if isinstance(parsed, dict) and parsed.get("errors"):
        error_data = parsed["errors"]
        error_message = str(error_data)
        # Detect stale persisted query hashes — Spotify rotates these periodically
        lowered = error_message.lower()
        if "persistedquerynotfound" in lowered or "persisted_query_not_found" in lowered or "persisted query" in lowered:
            raise SpotifyAPIError(
                f"Spotify API persisted query hash appears stale or invalid. "
                f"This typically means Spotify updated their API and the hardcoded SHA256 hashes "
                f"need updating. Error detail: {error_message}. "
                f"Please file an issue at https://github.com/rabilrbl/music-liked-sync/issues"
            )
        raise SpotifyAPIError(f"Spotify Web Player pathfinder returned errors: {error_data}")
    return parsed


def spotify_graphql_track_to_api_track(wrapper: dict) -> dict | None:
    data = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else wrapper
    if not isinstance(data, dict) or data.get("__typename") not in {"Track", None}:
        return None
    uri = str(wrapper.get("_uri") or data.get("uri") or data.get("_uri") or "")
    track_id = str(data.get("id") or (uri.split(":")[-1] if uri else ""))
    name = data.get("name")
    if not track_id or not name:
        return None
    artists = []
    for artist in ((data.get("artists") or {}).get("items") or []):
        if isinstance(artist, dict):
            artist_name = ((artist.get("profile") or {}).get("name") or artist.get("name"))
            if artist_name:
                artists.append({"name": artist_name})
    album_data = data.get("albumOfTrack") or data.get("album") or {}
    duration = data.get("duration") or {}
    duration_ms = data.get("duration_ms") or duration.get("totalMilliseconds")
    return {
        "id": track_id,
        "name": name,
        "artists": artists,
        "duration_ms": duration_ms,
        "album": {"name": album_data.get("name")},
    }


def spotify_graphql_library_item_to_api_item(item: dict) -> dict | None:
    track = item.get("track") if isinstance(item.get("track"), dict) else None
    if not track:
        return None
    parsed = spotify_graphql_track_to_api_track(track)
    return {"track": parsed} if parsed else None


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


def is_spotify_query_too_long_error(exc: BaseException) -> bool:
    return "query exceeds maximum length" in str(exc).lower()


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


class SpotifyWebClient:
    def __init__(self, token_provider: Callable[[], SpotifyWebSessionState | tuple[str, str]]) -> None:
        self._token_provider = token_provider
        self._state = self._normalize_state(token_provider())

    @staticmethod
    def _normalize_state(state: SpotifyWebSessionState | tuple[str, str]) -> SpotifyWebSessionState:
        if isinstance(state, SpotifyWebSessionState):
            return state
        access_token, user_agent = state
        return SpotifyWebSessionState(access_token=access_token, user_agent=user_agent)

    def _refresh_token(self) -> None:
        self._state = self._normalize_state(self._token_provider())

    def _pathfinder(self, payload: dict):
        try:
            return spotify_pathfinder_request_json(payload, state=self._state)
        except SpotifyAPIError as exc:
            if exc.http_status != 401:
                raise
            self._refresh_token()
            return spotify_pathfinder_request_json(payload, state=self._state)

    def current_user_saved_tracks(self, *, limit: int, offset: int, market: str):
        del market
        payload = {
            "variables": {"offset": offset, "limit": limit},
            "operationName": "fetchLibraryTracks",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240",
                }
            },
        }
        data = self._pathfinder(payload)
        tracks = (((data.get("data") or {}).get("me") or {}).get("library") or {}).get("tracks") or {}
        items = []
        for item in tracks.get("items") or []:
            parsed = spotify_graphql_library_item_to_api_item(item)
            if parsed:
                items.append(parsed)
        return {"items": items, "total": int(tracks.get("totalCount") or offset + len(items))}

    def search(self, *, q: str, type: str, limit: int, market: str):
        del market
        if type != "track":
            raise ValueError("SpotifyWebClient only supports track search")
        payload = {
            "variables": {
                "query": q,
                "limit": max(limit, 10),
                "offset": 0,
                "numberOfTopResults": max(limit, 10),
                "includeArtistHasConcertsField": False,
                "includeAudiobooks": True,
                "includeAuthors": False,
                "includePreReleases": True,
                "includeEpisodeContentRatingsV2": False,
                "sectionFilters": ["GENERIC", "VIDEO_CONTENT"],
            },
            "operationName": "searchTopResultsList",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "75a88491b7c54a02065a24d6e836121ab20ca42d1bede25a0e06fe5018033ffe",
                }
            },
        }
        data = self._pathfinder(payload)
        tracks: list[dict] = []
        self._collect_graphql_tracks(data, tracks)
        deduped: list[dict] = []
        seen: set[str] = set()
        for track in tracks:
            track_id = str(track.get("id") or "")
            if track_id and track_id not in seen:
                seen.add(track_id)
                deduped.append(track)
            if len(deduped) >= limit:
                break
        return {"tracks": {"items": deduped}}

    @staticmethod
    def _collect_graphql_tracks(value, tracks: list[dict]) -> None:
        if isinstance(value, dict):
            if value.get("__typename") == "TrackResponseWrapper":
                parsed = spotify_graphql_track_to_api_track(value)
                if parsed:
                    tracks.append(parsed)
                return
            for child in value.values():
                SpotifyWebClient._collect_graphql_tracks(child, tracks)
        elif isinstance(value, list):
            for child in value:
                SpotifyWebClient._collect_graphql_tracks(child, tracks)

    def current_user_saved_tracks_add(self, *, tracks: Sequence[str]):
        uris = [track if str(track).startswith("spotify:track:") else f"spotify:track:{track}" for track in tracks]
        payload = {
            "variables": {"libraryItemUris": uris},
            "operationName": "addToLibrary",
            "extensions": {
                "persistedQuery": {
                    "version": 1,
                    "sha256Hash": "7c5a69420e2bfae3da5cc4e14cbc8bb3f6090f80afc00ffc179177f19be3f33d",
                }
            },
        }
        return self._pathfinder(payload)


class SpotifyBackend:
    def __init__(
        self,
        *,
        market: str = DEFAULT_MARKET,
        web_session_dir: Path | str,
        web_headless: bool = False,
        web_login_timeout_seconds: float = DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT,
        lock_path: Path | str,
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
            )
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
        # Note: verbose logging for search is handled in resolve_matches
        for query in build_spotify_search_queries(wanted):
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
