import pytest
from music_liked_sync.models import Track
from music_liked_sync.utils import (
    normalize_text,
    normalize_key,
    normalize_artist,
    best_match,
    artist_matches,
    track_similarity,
    primary_search_artist,
    truncate_query,
    unique_by_key,
    batched,
    sleep_between_batches,
    cookie_value,
    playwright_cookie_header,
    read_json_object,
)


def test_normalize_key_ignores_case_punctuation_and_common_suffixes():
    assert normalize_key("Need Your Love - Remastered 2011", ["OneRepublic"]) == normalize_key(
        "need your love", ["one republic"]
    )


def test_artist_matches_edge_cases():
    assert not artist_matches([], ["Artist"])
    assert not artist_matches(["Artist"], [])
    assert artist_matches(["Alex Warren"], ["alexwarren"])
    assert artist_matches(["Alex Warren"], ["alxe warren"])  # SequenceMatcher branch (0.9 ratio)
    assert not artist_matches(["Alex Warren"], ["Someone Else"])


def test_track_similarity_with_duration():
    t1 = Track(title="Song", artists=("Artist",), source_id="1", duration_ms=180000)
    t2 = Track(title="Song", artists=("Artist",), source_id="2", duration_ms=181000)
    t3 = Track(title="Song", artists=("Artist",), source_id="3", duration_ms=220000)

    assert track_similarity(t1, t2) > track_similarity(t1, t3)


def test_best_match_with_candidates():
    wanted = Track(title="Fever Dream", artists=("Alex Warren",), source_id="spotify:track:1")
    assert best_match(wanted, []) is None

    # Similarity match (not exact key match)
    candidates = [
        Track(title="Fever Dreem", artists=("Alex Warren",), source_id="yt-video-1"),
    ]
    assert best_match(wanted, candidates).source_id == "yt-video-1"

    # Low score rejection
    assert best_match(wanted, [Track(title="Other", artists=("Other",), source_id="3")]) is None


def test_primary_search_artist():
    assert primary_search_artist(["DJ Raahul Pai, Ravi Sharma"]) == "DJ Raahul Pai"
    assert primary_search_artist([" - ", "Actual Artist"]) == "Actual Artist"
    assert primary_search_artist([]) == ""


def test_truncate_query():
    long_query = "a " * 150
    truncated = truncate_query(long_query, limit=10)
    assert len(truncated) <= 10
    assert truncate_query("short") == "short"


def test_unique_by_key():
    tracks = [
        Track(title="S1", artists=("A1",), source_id="1"),
        Track(title="s1", artists=("a1",), source_id="2"),
    ]
    assert len(unique_by_key(tracks)) == 1


def test_batched_errors():
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        batched([1, 2], 0)


def test_read_json_object_errors(tmp_path):
    p = tmp_path / "test.json"
    p.write_text("not json")
    assert read_json_object(p) == {}

    p.write_text("[]")  # Not a dict
    assert read_json_object(p) == {}


# --- normalize_text branch coverage ---

def test_normalize_text_ampersand():
    assert "and" in normalize_text("rock & roll")


def test_normalize_text_abbreviations():
    assert normalize_text("u r great") == "you are great"
    assert normalize_text("tea w/milk") == "tea withmilk"
    assert normalize_text("tea w/o milk") == "tea without milk"


def test_normalize_text_coke_studio():
    result = normalize_text("coke studio | season 14 | pasoori")
    assert "pasoori" in result
    assert "coke studio" not in result
    assert "season" not in result


def test_normalize_text_artist_prefix_stripping():
    result = normalize_text("imagine dragons - believer", artists=["Imagine Dragons"])
    assert "imagine dragons" not in result
    assert "believer" in result


def test_normalize_text_pipe_delimiter():
    result = normalize_text("song name | metadata info")
    assert "metadata" not in result
    assert "song name" in result


def test_normalize_text_feat_removal():
    result = normalize_text("song feat. someone else")
    assert "feat" not in result
    assert "someone" not in result
    assert "song" in result


def test_normalize_text_parenthesized_content():
    result = normalize_text("song (deluxe edition) title")
    assert "deluxe" not in result


def test_normalize_text_bracketed_content():
    result = normalize_text("song [live] title")
    assert "live" not in result


def test_normalize_text_common_suffix():
    result = normalize_text("song remastered 2011")
    assert "remastered" not in result


def test_normalize_text_unicode_normalization():
    assert normalize_text("café") == "cafe"


# --- normalize_artist ---

def test_normalize_artist_removes_spacing():
    assert normalize_artist("Imagine Dragons") == "imagine dragons".replace(" ", "")


# --- best_match exact key path ---

def test_best_match_exact_key_match():
    wanted = Track(title="Song", artists=("Artist",), source_id="1")
    candidate = Track(title="Song", artists=("Artist",), source_id="2")
    result = best_match(wanted, [candidate])
    assert result is candidate


# --- track_similarity missing duration ---

def test_track_similarity_without_duration():
    t1 = Track(title="Song", artists=("A",), source_id="1")
    t2 = Track(title="Song", artists=("A",), source_id="2")
    score = track_similarity(t1, t2)
    assert score > 0


# --- batched normal operation ---

def test_batched_normal():
    assert batched([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert batched([], 3) == []
    assert batched([1], 5) == [[1]]


# --- sleep_between_batches ---

def test_sleep_between_batches_skips_last():
    sleeps = []
    sleep_between_batches(0, 3, 1.0, sleeps.append)
    sleep_between_batches(1, 3, 1.0, sleeps.append)
    sleep_between_batches(2, 3, 1.0, sleeps.append)  # last batch, skip
    assert sleeps == [1.0, 1.0]


def test_sleep_between_batches_zero_delay():
    sleeps = []
    sleep_between_batches(0, 3, 0.0, sleeps.append)
    assert sleeps == []


# --- cookie_value ---

def test_cookie_value_extract():
    assert cookie_value("SID=abc; __Secure-3PAPISID=xyz", "__Secure-3PAPISID") == "xyz"
    assert cookie_value("SID=abc", "missing") is None


def test_cookie_value_empty_header():
    assert cookie_value("", "SID") is None


# --- playwright_cookie_header ---

def test_playwright_cookie_header():
    cookies = [
        {"name": "SID", "value": "abc", "domain": ".example.com"},
        {"name": "APISID", "value": "def", "domain": ".example.com"},
    ]
    header = playwright_cookie_header(cookies)
    assert "APISID=def" in header
    assert "SID=abc" in header


def test_playwright_cookie_header_skips_empty_name():
    cookies = [{"name": "", "value": "abc"}, {"name": "SID", "value": "def"}]
    header = playwright_cookie_header(cookies)
    assert header == "SID=def"


def test_playwright_cookie_header_empty():
    assert playwright_cookie_header([]) == ""
