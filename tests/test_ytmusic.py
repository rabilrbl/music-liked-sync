import pytest
import json
from music_liked_sync.models import FatalSearchError, Track
from music_liked_sync.ytmusic import (
    YTMusicBackend,
    parse_ytm_track,
    build_yt_browser_auth_headers,
    yt_sapisid_authorization,
    is_ytm_auth_error,
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
    except FatalSearchError as exc:
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
    except FatalSearchError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected expired auth error")

    assert "YouTube Music auth appears expired" in message
    assert "persistent browser session" in message


# --- parse_ytm_track edge cases ---

def test_parse_ytm_track_string_artists():
    item = {"title": "Song", "videoId": "abc", "artists": ["Artist One", "Artist Two"]}
    track = parse_ytm_track(item)
    assert track.artists == ("Artist One", "Artist Two")


def test_parse_ytm_track_with_duration_seconds():
    item = {"title": "Song", "videoId": "abc", "artists": [], "duration_seconds": 180}
    track = parse_ytm_track(item)
    assert track.duration_ms == 180000


def test_parse_ytm_track_with_album():
    item = {"title": "Song", "videoId": "abc", "artists": [], "album": {"name": "Album1"}}
    track = parse_ytm_track(item)
    assert track.album == "Album1"


def test_parse_ytm_track_album_not_dict():
    item = {"title": "Song", "videoId": "abc", "artists": [], "album": "string_album"}
    track = parse_ytm_track(item)
    assert track.album is None


def test_parse_ytm_track_missing_video_id():
    assert parse_ytm_track({"title": "Song", "artists": []}) is None


def test_parse_ytm_track_missing_title():
    assert parse_ytm_track({"videoId": "abc", "artists": []}) is None


def test_parse_ytm_track_entity_id_fallback():
    item = {"title": "Song", "entityId": "xyz", "artists": []}
    track = parse_ytm_track(item)
    assert track.source_id == "xyz"


def test_parse_ytm_track_mixed_artist_types():
    item = {
        "title": "Song",
        "videoId": "abc",
        "artists": [{"name": "Dict Artist"}, "String Artist"],
    }
    track = parse_ytm_track(item)
    assert track.artists == ("Dict Artist", "String Artist")


# --- is_ytm_auth_error ---

def test_is_ytm_auth_error_sign_in_liked():
    assert is_ytm_auth_error(KeyError("Sign in to get your liked tracks")) is True


def test_is_ytm_auth_error_401_signed_in():
    assert is_ytm_auth_error(Exception("HTTP 401: You must be signed in")) is True


def test_is_ytm_auth_error_401_no_signed_in():
    assert is_ytm_auth_error(Exception("HTTP 401: Unauthorized token")) is False


def test_is_ytm_auth_error_other_key_error():
    assert is_ytm_auth_error(KeyError("some other key error")) is False


# --- yt_sapisid_authorization missing cookie ---

def test_yt_sapisid_authorization_missing_cookie():
    with pytest.raises(RuntimeError, match="missing __Secure-3PAPISID"):
        yt_sapisid_authorization("SID=abc; APISID=def")


# --- build_yt_browser_auth_headers empty cookie ---

def test_build_yt_browser_auth_headers_empty_cookie():
    with pytest.raises(RuntimeError, match="no cookies"):
        build_yt_browser_auth_headers("", user_agent="ua")


def test_build_yt_browser_auth_headers_whitespace_cookie():
    with pytest.raises(RuntimeError, match="no cookies"):
        build_yt_browser_auth_headers("   ", user_agent="ua")


# --- retry_ytm_call JSONDecodeError retry ---

def test_retry_ytm_call_retries_json_decode_error():
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise json.JSONDecodeError("bad", "", 0)
        return "ok"
    sleeps = []
    result = retry_ytm_call(fn, label="test", sleep_fn=sleeps.append)
    assert result == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_retry_ytm_call_non_auth_key_error_reraises():
    with pytest.raises(KeyError, match="some other key"):
        retry_ytm_call(lambda: (_ for _ in ()).throw(KeyError("some other key")), label="test")
