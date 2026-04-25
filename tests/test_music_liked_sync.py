from music_liked_sync import Track, best_match, build_arg_parser, normalize_key, parse_ytm_track


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
