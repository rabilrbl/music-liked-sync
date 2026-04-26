import music_liked_sync
from music_liked_sync import (
    Track,
    SpotifyBackend,
    SyncCache,
    YTMusicBackend,
    best_match,
    build_arg_parser,
    default_yt_auth_file,
    load_spotify_config,
    normalize_key,
    parse_ytm_track,
    resolve_matches,
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


def test_parser_defaults_to_all_missing_with_configurable_batches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    args = build_arg_parser().parse_args([])
    assert args.max_add is None
    assert args.batch_size == 50
    assert args.batch_delay == 1.0
    assert args.yt_auth == "auto"
    assert args.yt_auth_file == "auth/oauth.json"


def test_default_yt_auth_file_prefers_browser_headers_when_available(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "oauth.json").write_text('{"refresh_token": "x"}')
    (auth_dir / "browser.json").write_text('{"cookie": "x", "authorization": "x"}')

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
    assert "ytmusicapi browser" in message


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

    status = music_liked_sync.main([])

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
