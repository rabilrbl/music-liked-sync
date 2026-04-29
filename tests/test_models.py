from music_liked_sync.models import Track, SpotifyWebSessionState, SearchResult


def test_track_display_with_artists():
    t = Track(title="Believer", artists=("Imagine Dragons",), source_id="1")
    assert t.display == "Believer — Imagine Dragons"


def test_track_display_with_multiple_artists():
    t = Track(title="Song", artists=("A", "B"), source_id="2")
    assert t.display == "Song — A, B"


def test_track_display_no_artists():
    t = Track(title="Mystery", artists=(), source_id="3")
    assert t.display == "Mystery — Unknown Artist"


def test_track_is_frozen():
    t = Track(title="T", artists=("A",), source_id="1")
    try:
        t.title = "changed"
        raise AssertionError("Track should be frozen")
    except AttributeError:
        pass


def test_track_equality():
    t1 = Track(title="T", artists=("A",), source_id="1")
    t2 = Track(title="T", artists=("A",), source_id="1")
    assert t1 == t2


def test_spotify_web_session_state_fields():
    state = SpotifyWebSessionState(
        access_token="tok", user_agent="ua", client_token="ct", app_version="1.0"
    )
    assert state.access_token == "tok"
    assert state.user_agent == "ua"
    assert state.client_token == "ct"
    assert state.app_version == "1.0"


def test_spotify_web_session_state_optional_fields():
    state = SpotifyWebSessionState(access_token="tok", user_agent="ua")
    assert state.client_token is None
    assert state.app_version is None


def test_search_result_fields():
    t = Track(title="T", artists=("A",), source_id="1")
    sr = SearchResult(wanted=t, match=None, search_failed=False, error_summary="err")
    assert sr.wanted is t
    assert sr.match is None
    assert not sr.search_failed
    assert sr.error_summary == "err"


def test_search_result_default_error_summary():
    t = Track(title="T", artists=("A",), source_id="1")
    sr = SearchResult(wanted=t, match=None, search_failed=False)
    assert sr.error_summary == ""