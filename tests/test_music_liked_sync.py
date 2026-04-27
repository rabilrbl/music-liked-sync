import music_liked_sync
import music_liked_sync.spotify
import music_liked_sync.ytmusic
from music_liked_sync.models import Track
from music_liked_sync.spotify import SpotifyBackend
from music_liked_sync.cache import SyncCache
from music_liked_sync.ytmusic import YTMusicBackend, parse_ytm_track, build_yt_browser_auth_headers, retry_ytm_call
from music_liked_sync.utils import normalize_key, best_match
from music_liked_sync.spotify import build_spotify_search_queries
from music_liked_sync.sync import resolve_matches
from music_liked_sync.cli import build_arg_parser
from music_liked_sync.spotify import browser_session_lock


def test_normalize_key_ignores_case_punctuation_and_common_suffixes():
    assert normalize_key("Need Your Love - Remastered 2011", ["OneRepublic"]) == normalize_key(
        "need your love", ["one republic"]
    )


def test_best_match_accepts_matching_title_and_artist():
    wanted = Track(title="Fever Dream", artists=("Alex Warren",), source_id="spotify:track:1")
    candidates = [
        Track(title="Fever Dream", artists=("Alex Warren",), source_id="yt-video-1"),
        Track(title="Fever Dream", artists=("Someone Else",), source_id="yt-video-2"),
    ]
    assert best_match(wanted, candidates).source_id == "yt-video-1"


def test_best_match_rejects_wrong_artist_even_with_same_title():
    wanted = Track(title="Fever Dream", artists=("Alex Warren",), source_id="spotify:track:1")
    candidates = [Track(title="Fever Dream", artists=("Someone Else",), source_id="yt-video-2")]
    assert best_match(wanted, candidates) is None


def test_parse_ytm_track_handles_artists_list():
    item = {"title": "Need Your Love", "videoId": "abc", "artists": [{"name": "OneRepublic"}]}
    assert parse_ytm_track(item) == Track(title="Need Your Love", artists=("OneRepublic",), source_id="abc")


def test_parser_rejects_removed_legacy_auth_flags():
    for argv in (
        ["--spotify-auth", "web-session"],
        ["--spotify-auth", "oauth"],
        ["--yt-auth", "browser-session"],
        ["--yt-auth", "browser"],
        ["--yt-auth-file", "auth/browser.json"],
        ["--yt-browser-session-dir", "auth/session"],
        ["--yt-browser-headless"],
        ["--yt-browser-login-timeout", "12"],
        ["--yt-refresh-browser-auth"],
        ["--spotify-web-session-dir", "auth/spotify-web"],
        ["--spotify-web-headless"],
        ["--spotify-web-login-timeout", "15"],
    ):
        try:
            build_arg_parser().parse_args(argv)
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError(f"expected parser to reject removed auth flag(s): {argv}")


def test_parser_defaults_are_session_only_without_auth_cli_knobs():
    args = build_arg_parser().parse_args([])
    assert args.max_add is None
    assert args.batch_size == 50
    assert args.batch_delay == 1.0
    assert args.market == "IN"


def test_spotify_web_client_retries_once_after_401(monkeypatch):
    calls = []

    def fake_request(payload, *, state):
        calls.append((payload["operationName"], state.access_token, state.user_agent, payload["variables"]))
        if state.access_token == "expired-token":
            raise music_liked_sync.spotify.SpotifyAPIError("expired", http_status=401)
        return {"data": {"me": {"library": {"tracks": {"items": [], "totalCount": 0}}}}}

    tokens = iter([("expired-token", "ua1"), ("fresh-token", "ua2")])
    monkeypatch.setattr(music_liked_sync.spotify, "spotify_pathfinder_request_json", fake_request)

    client = music_liked_sync.spotify.SpotifyWebClient(lambda: next(tokens))

    assert client.current_user_saved_tracks(limit=1, offset=0, market="IN") == {"items": [], "total": 0}
    assert calls == [
        ("fetchLibraryTracks", "expired-token", "ua1", {"offset": 0, "limit": 1}),
        ("fetchLibraryTracks", "fresh-token", "ua2", {"offset": 0, "limit": 1}),
    ]


def test_safe_page_user_agent_falls_back_when_page_is_navigating():
    class NavigatingPage:
        def evaluate(self, expression):
            raise RuntimeError("Execution context was destroyed, most likely because of a navigation")

    assert music_liked_sync.spotify._safe_page_user_agent(NavigatingPage()) == music_liked_sync.spotify.DEFAULT_BROWSER_USER_AGENT


def test_spotify_web_token_from_payload_rejects_anonymous_token():
    try:
        music_liked_sync.spotify.spotify_web_token_from_payload({"accessToken": "anon", "isAnonymous": True})
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected anonymous token rejection")

    assert "anonymous token" in message
    assert music_liked_sync.spotify.spotify_web_token_from_payload({"accessToken": "user-token", "isAnonymous": False}) == "user-token"


def test_parser_has_no_auth_selector_attributes():
    args = build_arg_parser().parse_args([])
    assert not hasattr(args, "yt_auth")
    assert not hasattr(args, "yt_auth_file")
    assert not hasattr(args, "yt_browser_session_dir")
    assert not hasattr(args, "spotify_auth")
    assert not hasattr(args, "spotify_web_session_dir")


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


def test_build_spotify_search_queries_uses_primary_artist_from_collapsed_artist_blob():
    wanted = Track(
        title="Party Mashup",
        artists=("DJ Raahul Pai, Ravi Sharma, Aditi Singh Sharma and Yasser Desai",),
        source_id="yt1",
    )

    queries = build_spotify_search_queries(wanted)

    assert queries[0] == "track:Party Mashup artist:DJ Raahul Pai"
    assert "track:Party Mashup" in queries


def test_ytmusic_backend_resolves_single_auth_mode():
    backend = YTMusicBackend.__new__(YTMusicBackend)
    backend.mode = "browser-session"
    assert backend.mode == "browser-session"


def test_spotify_save_tracks_uses_configurable_batches():
    class FakeSpotifyClient:
        def __init__(self):
            self.calls = []

        def current_user_saved_tracks_add(self, tracks):
            self.calls.append(list(tracks))

    backend = SpotifyBackend.__new__(SpotifyBackend)
    backend.mode = "web-session"
    backend.client = FakeSpotifyClient()
    tracks = [Track(title=f"Song {i}", artists=("Artist",), source_id=f"spotify:track:{i}") for i in range(5)]
    sleeps = []

    backend.save_tracks(tracks, batch_size=2, batch_delay=0.25, sleep_fn=sleeps.append)

    assert backend.client.calls == [["0", "1"], ["2", "3"], ["4"]]
    assert sleeps == [0.25, 0.25]


def test_youtube_likes_are_batched_with_configurable_delay():
    class FakeYTMusicClient:
        def __init__(self):
            self.calls = []

        def rate_song(self, video_id, rating):
            self.calls.append((video_id, rating))

    backend = YTMusicBackend.__new__(YTMusicBackend)
    backend.client = FakeYTMusicClient()
    tracks = [Track(title=f"Song {i}", artists=("Artist",), source_id=f"yt{i}") for i in range(5)]
    sleeps = []

    backend.like_tracks(tracks, batch_size=2, batch_delay=0.25, sleep_fn=sleeps.append)

    assert backend.client.calls == [(f"yt{i}", "LIKE") for i in range(5)]
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


def test_main_returns_2_when_browser_session_setup_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("browser setup failed")

    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", fail)

    status = music_liked_sync.cli.main([])

    captured = capsys.readouterr()
    assert status == 2
    assert "browser setup failed" in captured.err


def test_main_returns_2_when_youtube_write_auth_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    source = Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    target = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")

    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {"cookie": "SID=x", "authorization": "SAPISIDHASH x", "x-goog-authuser": "0"})

    class FakeSpotifyBackend:
        def __init__(self, **kwargs):
            self.mode = "web-session"

        def liked_tracks(self):
            return [source]

    class FakeYTMusicBackend:
        mode = "browser-session"

        def __init__(self, *args, **kwargs):
            pass

        def liked_tracks(self):
            return []

        def search_track(self, wanted):
            return [target]

        def like_tracks(self, tracks, **kwargs):
            raise RuntimeError("YouTube Music auth appears expired or signed out")

    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", FakeSpotifyBackend)
    monkeypatch.setattr(music_liked_sync.cli, "YTMusicBackend", FakeYTMusicBackend)
    monkeypatch.setattr(music_liked_sync.cli, "resolve_matches", lambda *args, **kwargs: ([(source, target)], []))

    status = music_liked_sync.cli.main(["--spotify-to-ytm", "--apply"])

    captured = capsys.readouterr()
    assert status == 2
    assert "YouTube Music auth appears expired" in captured.err


def test_browser_session_lock_blocks_second_lock(tmp_path):
    lock_path = tmp_path / "locks" / "yt.lock"
    with browser_session_lock(lock_path):
        try:
            with browser_session_lock(lock_path):
                raise AssertionError("second lock acquisition should fail")
        except RuntimeError as exc:
            assert "already active" in str(exc)


def test_sync_cache_roundtrip_store_lookup_and_mark_liked(tmp_path):
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")
    source = Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    target = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")

    assert cache.get_match("spotify_to_ytm", source) is None

    cache.store_match("spotify_to_ytm", source, target)
    loaded = cache.get_match("spotify_to_ytm", source)
    assert loaded is not None
    assert loaded.source_id == "yt1"

    assert not cache.is_liked("ytm", "yt1")
    cache.mark_liked("ytm", "yt1")
    assert cache.is_liked("ytm", "yt1")


def test_resolve_matches_continues_when_search_errors_transiently():
    wanted = [Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")]

    def search_fn(_track):
        raise ValueError("temporary non-json response")

    matched, unmatched = resolve_matches(wanted, search_fn, None, "Spotify → YTM")

    assert matched == []
    assert unmatched == wanted


def test_resolve_matches_uses_cache_before_search(tmp_path):
    wanted = [Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")]
    cached_match = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")
    cache.store_match("spotify_to_ytm", wanted[0], cached_match)

    calls = {"search": 0}

    def search_fn(_track):
        calls["search"] += 1
        return []

    matched, unmatched = resolve_matches(wanted, search_fn, None, "Spotify → YTM", cache=cache, cache_direction="spotify_to_ytm")

    assert len(matched) == 1
    assert matched[0][1].source_id == "yt1"
    assert unmatched == []
    assert calls["search"] == 0


def test_resolve_matches_persists_new_match_to_cache(tmp_path):
    wanted = [Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")]
    discovered = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")

    def search_fn(_track):
        return [discovered]

    matched, unmatched = resolve_matches(wanted, search_fn, None, "Spotify → YTM", cache=cache, cache_direction="spotify_to_ytm")

    assert len(matched) == 1
    assert unmatched == []
    loaded = cache.get_match("spotify_to_ytm", wanted[0])
    assert loaded is not None
    assert loaded.source_id == "yt1"


def test_parser_accepts_sync_cache_flags():
    args = build_arg_parser().parse_args(
        [
            "--cache-db",
            "state/sync.sqlite3",
            "--no-cache-read",
            "--no-cache-write",
        ]
    )
    assert args.cache_db == "state/sync.sqlite3"
    assert args.cache_read is False
    assert args.cache_write is False
