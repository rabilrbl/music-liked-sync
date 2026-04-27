import music_liked_sync.cli
from music_liked_sync.models import Track
from music_liked_sync.cli import build_arg_parser


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


def test_main_returns_2_when_youtube_write_auth_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    source = Track(
        title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1"
    )
    target = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")

    monkeypatch.setattr(
        music_liked_sync.cli,
        "ensure_yt_browser_auth_from_session",
        lambda **kwargs: {
            "cookie": "SID=x",
            "authorization": "SAPISIDHASH x",
            "x-goog-authuser": "0",
        },
    )

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
    monkeypatch.setattr(
        music_liked_sync.cli,
        "resolve_matches",
        lambda *args, **kwargs: ([(source, target)], []),
    )

    status = music_liked_sync.cli.main(["--spotify-to-ytm", "--apply"])

    captured = capsys.readouterr()
    assert status == 2
    assert "YouTube Music auth appears expired" in captured.err


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
