import pytest
import music_liked_sync.spotify
from music_liked_sync.models import Track, SpotifyWebSessionState
from music_liked_sync.spotify import (
    SpotifyAPIError,
    SpotifyBackend,
    SpotifyWebClient,
    build_spotify_search_queries,
    parse_spotify_track,
    spotify_graphql_track_to_api_track,
    spotify_graphql_library_item_to_api_item,
    is_spotify_transient_error,
    is_spotify_query_too_long_error,
    spotify_retry_delay_seconds,
    retry_spotify_call,
)
from music_liked_sync.utils import browser_session_lock


def test_spotify_web_client_retries_once_after_401(monkeypatch):
    calls = []

    def fake_request(payload, *, state):
        calls.append(
            (
                payload["operationName"],
                state.access_token,
                state.user_agent,
                payload["variables"],
            )
        )
        if state.access_token == "expired-token":
            raise music_liked_sync.spotify.SpotifyAPIError("expired", http_status=401)
        return {"data": {"me": {"library": {"tracks": {"items": [], "totalCount": 0}}}}}

    tokens = iter([("expired-token", "ua1"), ("fresh-token", "ua2")])
    monkeypatch.setattr(
        music_liked_sync._spotify_client, "spotify_pathfinder_request_json", fake_request
    )

    client = music_liked_sync.spotify.SpotifyWebClient(lambda: next(tokens))

    assert client.current_user_saved_tracks(limit=1, offset=0, market="IN") == {
        "items": [],
        "total": 0,
    }
    assert calls == [
        ("fetchLibraryTracks", "expired-token", "ua1", {"offset": 0, "limit": 1}),
        ("fetchLibraryTracks", "fresh-token", "ua2", {"offset": 0, "limit": 1}),
    ]


def test_safe_page_user_agent_falls_back_when_page_is_navigating():
    class NavigatingPage:
        def evaluate(self, expression):
            raise RuntimeError(
                "Execution context was destroyed, most likely because of a navigation"
            )

    assert (
        music_liked_sync.utils.safe_page_user_agent(NavigatingPage())
        == music_liked_sync.constants.DEFAULT_BROWSER_USER_AGENT
    )


def test_spotify_web_token_from_payload_rejects_anonymous_token():
    try:
        music_liked_sync.spotify.spotify_web_token_from_payload(
            {"accessToken": "anon", "isAnonymous": True}
        )
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected anonymous token rejection")

    assert "anonymous token" in message
    assert (
        music_liked_sync.spotify.spotify_web_token_from_payload(
            {"accessToken": "user-token", "isAnonymous": False}
        )
        == "user-token"
    )


def test_build_spotify_search_queries_uses_primary_artist_from_collapsed_artist_blob():
    wanted = Track(
        title="Party Mashup",
        artists=("DJ Raahul Pai, Ravi Sharma, Aditi Singh Sharma and Yasser Desai",),
        source_id="yt1",
    )

    queries = build_spotify_search_queries(wanted)

    assert queries[0] == "track:Party Mashup artist:DJ Raahul Pai"
    assert "track:Party Mashup" in queries


def test_spotify_save_tracks_uses_configurable_batches():
    class FakeSpotifyClient:
        def __init__(self):
            self.calls = []

        def current_user_saved_tracks_add(self, tracks):
            self.calls.append(list(tracks))

    backend = SpotifyBackend.__new__(SpotifyBackend)
    backend.mode = "web-session"
    backend.client = FakeSpotifyClient()
    tracks = [
        Track(title=f"Song {i}", artists=("Artist",), source_id=f"spotify:track:{i}")
        for i in range(5)
    ]
    sleeps = []

    backend.save_tracks(tracks, batch_size=2, batch_delay=0.25, sleep_fn=sleeps.append)

    assert backend.client.calls == [["0", "1"], ["2", "3"], ["4"]]
    assert sleeps == [0.25, 0.25]


def test_browser_session_lock_blocks_second_lock(tmp_path):
    lock_path = tmp_path / "locks" / "yt.lock"
    with browser_session_lock(lock_path):
        try:
            with browser_session_lock(lock_path):
                raise AssertionError("second lock acquisition should fail")
        except RuntimeError as exc:
            assert "already active" in str(exc)


# --- SpotifyAPIError ---

def test_spotify_api_error_stores_status_and_headers():
    err = SpotifyAPIError("test error", http_status=429, headers={"Retry-After": "5"})
    assert err.http_status == 429
    assert err.headers == {"Retry-After": "5"}
    assert str(err) == "test error"


def test_spotify_api_error_defaults():
    err = SpotifyAPIError("msg")
    assert err.http_status is None
    assert err.headers == {}


# --- spotify_graphql_track_to_api_track ---

def test_spotify_graphql_track_to_api_track_basic():
    wrapper = {
        "data": {
            "__typename": "Track",
            "id": "abc123",
            "name": "Test Song",
            "uri": "spotify:track:abc123",
            "artists": {"items": [{"profile": {"name": "Artist1"}}]},
            "albumOfTrack": {"name": "Album1"},
            "duration": {"totalMilliseconds": 200000},
        }
    }
    result = spotify_graphql_track_to_api_track(wrapper)
    assert result is not None
    assert result["id"] == "abc123"
    assert result["name"] == "Test Song"
    assert result["artists"] == [{"name": "Artist1"}]
    assert result["duration_ms"] == 200000
    assert result["album"]["name"] == "Album1"


def test_spotify_graphql_track_to_api_track_missing_id():
    wrapper = {"data": {"__typename": "Track", "name": "Song"}}
    assert spotify_graphql_track_to_api_track(wrapper) is None


def test_spotify_graphql_track_to_api_track_missing_name():
    wrapper = {"data": {"__typename": "Track", "id": "123"}}
    assert spotify_graphql_track_to_api_track(wrapper) is None


def test_spotify_graphql_track_to_api_track_wrong_typename():
    wrapper = {"data": {"__typename": "Album", "id": "123", "name": "A"}}
    assert spotify_graphql_track_to_api_track(wrapper) is None


def test_spotify_graphql_track_to_api_track_uri_fallback():
    wrapper = {
        "_uri": "spotify:track:xyz",
        "data": {"__typename": None, "name": "Song"},
    }
    result = spotify_graphql_track_to_api_track(wrapper)
    assert result is not None
    assert result["id"] == "xyz"


# --- spotify_graphql_library_item_to_api_item ---

def test_spotify_graphql_library_item_to_api_item():
    item = {
        "track": {
            "data": {
                "__typename": "Track",
                "id": "1",
                "name": "Song",
                "uri": "spotify:track:1",
            }
        }
    }
    result = spotify_graphql_library_item_to_api_item(item)
    assert result is not None
    assert result["track"]["id"] == "1"


def test_spotify_graphql_library_item_to_api_item_no_track():
    assert spotify_graphql_library_item_to_api_item({}) is None
    assert spotify_graphql_library_item_to_api_item({"track": None}) is None


# --- parse_spotify_track ---

def test_parse_spotify_track_basic():
    item = {
        "track": {
            "id": "abc",
            "name": "Song",
            "artists": [{"name": "A1"}, {"name": "A2"}],
            "duration_ms": 180000,
            "album": {"name": "Album"},
        }
    }
    result = parse_spotify_track(item)
    assert result.title == "Song"
    assert result.artists == ("A1", "A2")
    assert result.source_id == "spotify:track:abc"
    assert result.duration_ms == 180000
    assert result.album == "Album"


def test_parse_spotify_track_missing_id():
    item = {"track": {"name": "Song"}}
    assert parse_spotify_track(item) is None


def test_parse_spotify_track_missing_name():
    item = {"track": {"id": "123"}}
    assert parse_spotify_track(item) is None


def test_parse_spotify_track_flat_item():
    item = {"id": "abc", "name": "Song", "artists": [{"name": "A"}]}
    result = parse_spotify_track(item)
    assert result.source_id == "spotify:track:abc"


def test_parse_spotify_track_empty_track():
    assert parse_spotify_track({"track": {}}) is None
    assert parse_spotify_track({"track": None}) is None


# --- is_spotify_transient_error ---

def test_is_spotify_transient_error():
    err = SpotifyAPIError("rate limited", http_status=429)
    assert is_spotify_transient_error(err) is True

    err500 = SpotifyAPIError("server error", http_status=500)
    assert is_spotify_transient_error(err500) is True

    err404 = SpotifyAPIError("not found", http_status=404)
    assert is_spotify_transient_error(err404) is False

    plain = RuntimeError("unknown")
    assert is_spotify_transient_error(plain) is False


# --- is_spotify_query_too_long_error ---

def test_is_spotify_query_too_long_error():
    assert is_spotify_query_too_long_error(RuntimeError("Query exceeds maximum length")) is True
    assert is_spotify_query_too_long_error(RuntimeError("other error")) is False


# --- spotify_retry_delay_seconds ---

def test_spotify_retry_delay_retry_after_header():
    err = SpotifyAPIError("rate limited", http_status=429, headers={"Retry-After": "10"})
    assert spotify_retry_delay_seconds(err, 1) == 10.0


def test_spotify_retry_delay_retry_after_capped():
    err = SpotifyAPIError("rate limited", http_status=429, headers={"Retry-After": "999"})
    assert spotify_retry_delay_seconds(err, 1) == 30.0  # SPOTIFY_MAX_RETRY_AFTER


def test_spotify_retry_delay_exponential_backoff():
    err = RuntimeError("transient")
    assert spotify_retry_delay_seconds(err, 1) == 2.0
    assert spotify_retry_delay_seconds(err, 3) == 6.0


# --- retry_spotify_call ---

def test_retry_spotify_call_success_first_try():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return "ok"
    result = retry_spotify_call(fn, label="test")
    assert result == "ok"
    assert calls["n"] == 1


def test_retry_spotify_call_retries_transient(monkeypatch):
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise SpotifyAPIError("429", http_status=429)
        return "ok"
    sleeps = []
    result = retry_spotify_call(fn, label="test", sleep_fn=sleeps.append)
    assert result == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_retry_spotify_call_raises_non_transient():
    err = SpotifyAPIError("forbidden", http_status=403)
    with pytest.raises(SpotifyAPIError):
        retry_spotify_call(lambda: (_ for _ in ()).throw(err), label="test")


def test_retry_spotify_call_raises_query_too_long():
    with pytest.raises(RuntimeError, match="Query exceeds maximum length"):
        retry_spotify_call(
            lambda: raise_runtime("Query exceeds maximum length"), label="test"
        )


def raise_runtime(msg):
    raise RuntimeError(msg)


# --- SpotifyWebClient._normalize_state ---

def test_normalize_state_from_tuple():
    state = SpotifyWebClient._normalize_state(("token123", "ua456"))
    assert isinstance(state, SpotifyWebSessionState)
    assert state.access_token == "token123"
    assert state.user_agent == "ua456"


def test_normalize_state_from_state_object():
    orig = SpotifyWebSessionState(access_token="tok", user_agent="ua")
    result = SpotifyWebClient._normalize_state(orig)
    assert result is orig


# --- SpotifyWebClient.search ---

def test_spotify_web_client_search_type_validation():
    client = SpotifyWebClient.__new__(SpotifyWebClient)
    with pytest.raises(ValueError, match="only supports track search"):
        client.search(q="test", type="album", limit=5, market="US")
