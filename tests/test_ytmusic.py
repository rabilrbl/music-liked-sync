from music_liked_sync.models import Track
from music_liked_sync.ytmusic import (
    YTMusicBackend,
    parse_ytm_track,
    build_yt_browser_auth_headers,
    retry_ytm_call,
)


def test_parse_ytm_track_handles_artists_list():
    item = {"title": "Need Your Love", "videoId": "abc", "artists": [{"name": "OneRepublic"}]}
    assert parse_ytm_track(item) == Track(
        title="Need Your Love", artists=("OneRepublic",), source_id="abc"
    )


def test_build_yt_browser_auth_headers_from_session_cookie():
    headers = build_yt_browser_auth_headers(
        "__Secure-3PAPISID=sapisid; SID=sid",
        user_agent="Mozilla/5.0 TestBrowser",
        timestamp=1700000000,
    )

    assert headers["cookie"] == "__Secure-3PAPISID=sapisid; SID=sid"
    assert headers["user-agent"] == "Mozilla/5.0 TestBrowser"
    assert headers["x-goog-authuser"] == "0"
    assert headers["origin"] == "https://music.youtube.com"
    assert headers["authorization"].startswith("SAPISIDHASH 1700000000_")


def test_ytmusic_backend_resolves_single_auth_mode():
    backend = YTMusicBackend.__new__(YTMusicBackend)
    backend.mode = "browser-session"
    assert backend.mode == "browser-session"


def test_youtube_likes_are_batched_with_configurable_delay():
    class FakeYTMusicClient:
        def __init__(self):
            self.calls = []

        def rate_song(self, video_id, rating):
            self.calls.append((video_id, rating))

    backend = YTMusicBackend.__new__(YTMusicBackend)
    backend.client = FakeYTMusicClient()
    tracks = [
        Track(title=f"Song {i}", artists=("Artist",), source_id=f"yt{i}") for i in range(5)
    ]
    sleeps = []

    backend.like_tracks(tracks, batch_size=2, batch_delay=0.25, sleep_fn=sleeps.append)

    assert sorted(backend.client.calls) == sorted([(f"yt{i}", "LIKE") for i in range(5)])

    assert sleeps == [0.25, 0.25]


def test_youtube_sign_in_response_reports_expired_browser_auth():
    class SignedOutYTMusicClient:
        def get_liked_songs(self, limit):
            raise KeyError("Sign in to listen to your liked tracks")

    backend = YTMusicBackend.__new__(YTMusicBackend)
    backend.client = SignedOutYTMusicClient()

    try:
        backend.liked_tracks()
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected expired auth error")

    assert "YouTube Music auth appears expired" in message
    assert "browser-session" in message


def test_youtube_401_response_reports_expired_browser_auth():
    class Unauthorized(Exception):
        pass

    def fail():
        raise Unauthorized(
            "Server returned HTTP 401: Unauthorized. You must be signed in to perform this operation."
        )

    try:
        retry_ytm_call(fail, label="YTM rate_song", sleep_fn=lambda seconds: None)
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected expired auth error")

    assert "YouTube Music auth appears expired" in message
    assert "persistent browser session" in message
