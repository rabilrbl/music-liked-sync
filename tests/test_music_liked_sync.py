from music_liked_sync import Track, SpotifyBackend, YTMusicBackend, best_match, build_arg_parser, normalize_key, parse_ytm_track


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


def test_parser_defaults_to_all_missing_with_configurable_batches():
    args = build_arg_parser().parse_args([])
    assert args.max_add is None
    assert args.batch_size == 50
    assert args.batch_delay == 1.0


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
