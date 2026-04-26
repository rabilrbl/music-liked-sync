import music_liked_sync
from music_liked_sync import (
    Track,
    CommandHeartbeat,
    SpotifyBackend,
    SyncCache,
    YTMusicBackend,
    best_match,
    build_arg_parser,
    build_spotify_search_queries,
    build_yt_browser_auth_headers,
    default_yt_auth_file,
    load_spotify_config,
    normalize_key,
    parse_ytm_track,
    resolve_matches,
    retry_ytm_call,
)


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


def test_parser_accepts_spotify_oauth_settings():
    args = build_arg_parser().parse_args(
        [
            "--spotify-auth",
            "oauth",
            "--spotify-client-id",
            "cid",
            "--spotify-client-secret",
            "secret",
            "--spotify-redirect-uri",
            "http://127.0.0.1:8888/callback",
            "--spotify-cache",
            ".cache-spotify",
        ]
    )
    assert args.spotify_auth == "oauth"
    assert args.spotify_client_id == "cid"
    assert args.spotify_client_secret == "secret"
    assert args.spotify_redirect_uri == "http://127.0.0.1:8888/callback"
    assert args.spotify_cache == ".cache-spotify"


def test_parser_accepts_spotify_pkce_without_client_secret():
    args = build_arg_parser().parse_args(
        [
            "--spotify-auth",
            "pkce",
            "--spotify-client-id",
            "cid",
            "--spotify-redirect-uri",
            "http://127.0.0.1:43827/spotify/callback",
            "--spotify-cache",
            ".cache-spotify-pkce",
        ]
    )
    assert args.spotify_auth == "pkce"
    assert args.spotify_client_id == "cid"
    assert args.spotify_client_secret is None
    assert args.spotify_redirect_uri == "http://127.0.0.1:43827/spotify/callback"
    assert args.spotify_cache == ".cache-spotify-pkce"


def test_spotify_auto_auth_prefers_pkce_when_client_id_has_no_secret():
    assert SpotifyBackend._resolve_auth_mode("auto", "cid", None) == "pkce"


def test_parser_defaults_to_persistent_youtube_browser_session_with_configurable_batches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    args = build_arg_parser().parse_args([])
    assert args.max_add is None
    assert args.batch_size == 50
    assert args.batch_delay == 1.0
    assert args.yt_auth == "browser-session"
    assert args.yt_auth_file == "auth/browser.json"
    assert args.yt_browser_session_dir == "auth/ytmusic-browser-session"


def test_default_yt_auth_file_uses_browser_session_auth_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "oauth.json").write_text('{"refresh_token": "***"}')

    assert default_yt_auth_file() == "auth/browser.json"


def test_parser_uses_local_spotify_config_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "spotify.json").write_text(
        '{"auth": "pkce", "client_id": "cid", "redirect_uri": "http://127.0.0.1:43827/spotify/callback", "cache": ".cache-spotify-pkce"}'
    )

    args = build_arg_parser().parse_args([])

    assert args.spotify_auth == "pkce"
    assert args.spotify_client_id == "cid"
    assert args.spotify_redirect_uri == "http://127.0.0.1:43827/spotify/callback"
    assert args.spotify_cache == ".cache-spotify-pkce"


def test_load_spotify_config_ignores_invalid_or_missing_files(tmp_path):
    missing = load_spotify_config(tmp_path / "missing.json")
    assert missing == {}

    invalid = tmp_path / "spotify.json"
    invalid.write_text("not json")
    assert load_spotify_config(invalid) == {}


def test_parser_accepts_browser_youtube_music_auth_file():
    args = build_arg_parser().parse_args(["--yt-auth", "browser", "--yt-auth-file", "auth/browser.json"])
    assert args.yt_auth == "browser"
    assert args.yt_auth_file == "auth/browser.json"


def test_parser_accepts_persistent_youtube_music_browser_session_flags():
    args = build_arg_parser().parse_args(
        [
            "--yt-auth",
            "browser-session",
            "--yt-browser-session-dir",
            "auth/session",
            "--yt-browser-login-timeout",
            "12",
            "--yt-refresh-browser-auth",
        ]
    )
    assert args.yt_auth == "browser-session"
    assert args.yt_browser_session_dir == "auth/session"
    assert args.yt_browser_login_timeout == 12.0
    assert args.yt_refresh_browser_auth is True


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


def test_parser_accepts_heartbeat_flags():
    args = build_arg_parser().parse_args(
        [
            "--heartbeat-command",
            "./auth/yt-heartbeat.sh",
            "--heartbeat-interval",
            "7",
            "--heartbeat-timeout",
            "3",
        ]
    )
    assert args.heartbeat_command == "./auth/yt-heartbeat.sh"
    assert args.heartbeat_interval == 7.0
    assert args.heartbeat_timeout == 3.0


def test_build_spotify_search_queries_uses_primary_artist_from_collapsed_artist_blob():
    wanted = Track(
        title="Party Mashup",
        artists=("DJ Raahul Pai, Ravi Sharma, Aditi Singh Sharma and Yasser Desai",),
        source_id="yt1",
    )

    queries = build_spotify_search_queries(wanted)

    assert queries[0] == "track:Party Mashup artist:DJ Raahul Pai"
    assert "track:Party Mashup" in queries


def test_command_heartbeat_runs_once_per_interval():
    calls = []
    now = {"value": 100.0}

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    heartbeat = CommandHeartbeat(
        "printf ok",
        interval_seconds=10.0,
        timeout_seconds=1.0,
        run_fn=lambda *args, **kwargs: calls.append((args, kwargs)) or Result(),
        time_fn=lambda: now["value"],
    )

    heartbeat.maybe_beat(force=True)
    now["value"] = 105.0
    heartbeat.maybe_beat()
    now["value"] = 111.0
    heartbeat.maybe_beat()

    assert len(calls) == 2


def test_resolve_matches_calls_heartbeat_between_items():
    wanted = [
        Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1"),
        Track(title="Demons", artists=("Imagine Dragons",), source_id="spotify:track:2"),
    ]
    discovered = {
        "Believer": Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1"),
        "Demons": Track(title="Demons", artists=("Imagine Dragons",), source_id="yt2"),
    }

    class FakeHeartbeat:
        def __init__(self):
            self.calls = 0

        def maybe_beat(self, *, force: bool = False):
            self.calls += 1

    heartbeat = FakeHeartbeat()

    matched, unmatched = resolve_matches(
        wanted,
        lambda track: [discovered[track.title]],
        None,
        "Spotify → YTM",
        heartbeat=heartbeat,
    )

    assert len(matched) == 2
    assert unmatched == []
    assert heartbeat.calls == 2


def test_ytmusic_backend_detects_browser_auth_file_even_when_oauth_env_exists(tmp_path):
    auth_file = tmp_path / "browser.json"
    auth_file.write_text('{"cookie":"SID=x", "authorization":"SAPISIDHASH y", "x-goog-authuser":"0"}')

    assert YTMusicBackend.resolve_auth_mode("auto", auth_file, "client-id", "client-secret") == "browser"


def test_ytmusic_backend_detects_oauth_auth_file(tmp_path):
    auth_file = tmp_path / "oauth.json"
    auth_file.write_text('{"refresh_token":"refresh", "access_token":"access"}')

    assert YTMusicBackend.resolve_auth_mode("auto", auth_file, "client-id", "client-secret") == "oauth"


def test_spotify_save_tracks_uses_configurable_batches():
    class FakeSpotifyClient:
        def __init__(self):
            self.calls = []

        def current_user_saved_tracks_add(self, tracks):
            self.calls.append(list(tracks))

    backend = SpotifyBackend.__new__(SpotifyBackend)
    backend.mode = "oauth"
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

    monkeypatch.setattr(music_liked_sync, "ensure_yt_browser_auth_from_session", fail)

    status = music_liked_sync.main(["--yt-auth", "browser-session"])

    captured = capsys.readouterr()
    assert status == 2
    assert "browser setup failed" in captured.err


def test_main_returns_2_when_youtube_write_auth_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    source = Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    target = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")

    class FakeSpotifyBackend:
        def __init__(self, **kwargs):
            pass

        def liked_tracks(self):
            return [source]

    class FakeYTMusicBackend:
        mode = "browser"

        def __init__(self, *args, **kwargs):
            pass

        def liked_tracks(self):
            return []

        def search_track(self, wanted):
            return [target]

        def like_tracks(self, tracks, **kwargs):
            raise RuntimeError("YouTube Music auth appears expired or signed out")

    monkeypatch.setattr(music_liked_sync, "SpotifyBackend", FakeSpotifyBackend)
    monkeypatch.setattr(music_liked_sync, "YTMusicBackend", FakeYTMusicBackend)
    monkeypatch.setattr(music_liked_sync, "resolve_matches", lambda *args, **kwargs: ([(source, target)], []))

    status = music_liked_sync.main(["--yt-auth", "browser", "--spotify-to-ytm", "--apply"])

    captured = capsys.readouterr()
    assert status == 2
    assert "YouTube Music auth appears expired" in captured.err


def test_main_returns_2_for_expired_youtube_auth(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    class FakeSpotifyBackend:
        def __init__(self, **kwargs):
            pass

        def liked_tracks(self):
            return []

    class FakeYTMusicBackend:
        mode = "browser"

        def __init__(self, *args, **kwargs):
            pass

        def liked_tracks(self):
            raise RuntimeError("YouTube Music auth appears expired or signed out")

    monkeypatch.setattr(music_liked_sync, "SpotifyBackend", FakeSpotifyBackend)
    monkeypatch.setattr(music_liked_sync, "YTMusicBackend", FakeYTMusicBackend)

    status = music_liked_sync.main(["--yt-auth", "browser"])

    captured = capsys.readouterr()
    assert status == 2
    assert "YouTube Music auth appears expired" in captured.err


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
