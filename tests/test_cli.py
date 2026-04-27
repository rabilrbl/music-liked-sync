import argparse
import pytest
import music_liked_sync.cli
from music_liked_sync.models import Track
from music_liked_sync.cli import build_arg_parser, positive_int, non_negative_float


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


def test_parser_has_no_auth_selector_attributes():
    args = build_arg_parser().parse_args([])
    assert not hasattr(args, "yt_auth")
    assert not hasattr(args, "yt_auth_file")
    assert not hasattr(args, "yt_browser_session_dir")
    assert not hasattr(args, "spotify_auth")
    assert not hasattr(args, "spotify_web_session_dir")


def test_main_returns_2_when_browser_session_setup_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def fail(*args, **kwargs):
        raise RuntimeError("browser setup failed")

    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", fail)

    status = music_liked_sync.cli.main([])

    captured = capsys.readouterr()
    assert status == 2
    assert "browser setup failed" in captured.err


def test_main_returns_2_when_spotify_init_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {})

    def fail(*args, **kwargs):
        raise RuntimeError("spotify fail")

    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", fail)
    assert music_liked_sync.cli.main([]) == 2
    assert "spotify fail" in capsys.readouterr().err


def test_main_returns_2_when_ytm_init_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {})
    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", lambda **kwargs: None)

    def fail(*args, **kwargs):
        raise ValueError("ytm init fail")

    monkeypatch.setattr(music_liked_sync.cli, "YTMusicBackend", fail)
    assert music_liked_sync.cli.main([]) == 2
    assert "ytm init fail" in capsys.readouterr().err


def test_main_returns_2_when_ytm_liked_tracks_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {})

    class MockSpotify:
        mode = "web-session"

        def __init__(self, **kwargs):
            pass

        def liked_tracks(self):
            return []

    class MockYTM:
        mode = "browser-session"

        def __init__(self, auth):
            pass

        def liked_tracks(self):
            raise RuntimeError("ytm fetch fail")

    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", MockSpotify)
    monkeypatch.setattr(music_liked_sync.cli, "YTMusicBackend", MockYTM)
    assert music_liked_sync.cli.main([]) == 2
    assert "ytm fetch fail" in capsys.readouterr().err


def test_main_returns_2_when_ytm_like_tracks_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    source = Track(title="S1", artists=("A1",), source_id="s1")
    target = Track(title="Y1", artists=("A1",), source_id="y1")
    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {})

    class MockSpotify:
        mode = "web-session"

        def __init__(self, **kwargs):
            pass

        def liked_tracks(self):
            return [source]

    class MockYTM:
        mode = "browser-session"

        def __init__(self, auth):
            pass

        def liked_tracks(self):
            return []

        def search_track(self, wanted):
            return [target]

        def like_tracks(self, tracks, **kwargs):
            raise RuntimeError("ytm like fail")

    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", MockSpotify)
    monkeypatch.setattr(music_liked_sync.cli, "YTMusicBackend", MockYTM)
    assert music_liked_sync.cli.main(["--apply", "--spotify-to-ytm"]) == 2
    assert "ytm like fail" in capsys.readouterr().err


def test_main_success_full_sync(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    spotify_track = Track(title="S1", artists=("A1",), source_id="s1")
    ytm_track = Track(title="Y1", artists=("A2",), source_id="y1")

    monkeypatch.setattr(music_liked_sync.cli, "ensure_yt_browser_auth_from_session", lambda **kwargs: {"cookie": "c"})

    class MockSpotify:
        mode = "web-session"

        def __init__(self, **kwargs):
            pass

        def liked_tracks(self):
            return [spotify_track]

        def search_track(self, wanted):
            return [spotify_track]

        def save_tracks(self, tracks, **kwargs):
            pass

    class MockYTM:
        mode = "browser-session"

        def __init__(self, auth):
            pass

        def liked_tracks(self):
            return [ytm_track]

        def search_track(self, wanted):
            return [ytm_track]

        def like_tracks(self, tracks, **kwargs):
            pass

    monkeypatch.setattr(music_liked_sync.cli, "SpotifyBackend", MockSpotify)
    monkeypatch.setattr(music_liked_sync.cli, "YTMusicBackend", MockYTM)

    status = music_liked_sync.cli.main(["--apply", "--cache-db", "cache.db"])
    assert status == 0

    # Check if report was written
    assert (tmp_path / "sync-report.json").exists()

    # Run again to test library cache read
    status = music_liked_sync.cli.main(["--apply", "--cache-db", "cache.db", "--cache-library-ttl", "3600"])
    assert status == 0


def test_positive_int_validator():
    assert positive_int("5") == 5
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int("0")
    with pytest.raises(argparse.ArgumentTypeError):
        positive_int("-1")


def test_non_negative_float_validator():
    assert non_negative_float("0.5") == 0.5
    assert non_negative_float("0") == 0.0
    with pytest.raises(argparse.ArgumentTypeError):
        non_negative_float("-0.1")


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
