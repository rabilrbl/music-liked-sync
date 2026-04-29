import music_liked_sync.spotify
from music_liked_sync.models import Track
from music_liked_sync.spotify import (
    SpotifyBackend,
    build_spotify_search_queries,
    browser_session_lock,
)


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
        music_liked_sync.spotify, "spotify_pathfinder_request_json", fake_request
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
