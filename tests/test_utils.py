import pytest
from music_liked_sync.models import Track
from music_liked_sync.utils import (
    normalize_key,
    best_match,
    artist_matches,
    track_similarity,
    primary_search_artist,
    truncate_query,
    unique_by_key,
    batched,
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
