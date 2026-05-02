import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence

from .constants import (
    SPOTIFY_MAX_RETRY_AFTER,
    SPOTIFY_RETRY_ATTEMPTS,
    SPOTIFY_RETRY_BASE_DELAY,
    SPOTIFY_WEB_ORIGIN,
    SPOTIFY_WEB_PATHFINDER_URL,
)
from .models import SpotifyWebSessionState, Track


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


# --- GraphQL data parsing ---

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


# --- Retry infrastructure ---

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


# --- HTTP client ---

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
