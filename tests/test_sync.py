from music_liked_sync.sync import resolve_matches, compute_missing
from music_liked_sync.models import Track
from music_liked_sync.cache import SyncCache


def test_compute_missing_preserves_duplicates_on_left_and_handles_fuzzy():
    t1 = Track(title="You And Me", artists=("Artist",), source_id="s1")
    t2 = Track(title="U and Me", artists=("Artist",), source_id="s2")
    t3 = Track(title="Different", artists=("Artist",), source_id="s3")
    
    # If t1 is in 'right', t2 should also be considered matched due to fuzzy matching
    missing = compute_missing([t1, t2, t3], [t1])
    assert len(missing) == 1
    assert missing[0].source_id == "s3"

    # Exact match check
    missing2 = compute_missing([t1], [t1])
    assert len(missing2) == 0


def test_resolve_matches_continues_when_search_errors_transiently():
    wanted = [
        Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    ]

    def search_fn(_track):
        raise ValueError("temporary non-json response")

    matched, unmatched = resolve_matches(wanted, search_fn, None, "Spotify → YTM")

    assert matched == []
    assert unmatched == wanted


def test_resolve_matches_raises_runtime_error():
    import pytest
    wanted = [Track(title="T", artists=("A",), source_id="1")]
    def search_fn(_track):
        raise RuntimeError("fatal")
    with pytest.raises(RuntimeError, match="fatal"):
        resolve_matches(wanted, search_fn, None, "test")


def test_resolve_matches_uses_cache_before_search(tmp_path):
    wanted = [
        Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    ]
    cached_match = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")
    cache.store_match("spotify_to_ytm", wanted[0], cached_match)

    calls = {"search": 0}

    def search_fn(_track):
        calls["search"] += 1
        return []

    matched, unmatched = resolve_matches(
        wanted,
        search_fn,
        None,
        "Spotify → YTM",
        cache=cache,
        cache_direction="spotify_to_ytm",
    )

    assert len(matched) == 1
    assert matched[0][1].source_id == "yt1"
    assert unmatched == []
    assert calls["search"] == 0


def test_resolve_matches_persists_new_match_to_cache(tmp_path):
    wanted = [
        Track(title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1")
    ]
    discovered = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")

    def search_fn(_track):
        return [discovered]

    matched, unmatched = resolve_matches(
        wanted,
        search_fn,
        None,
        "Spotify → YTM",
        cache=cache,
        cache_direction="spotify_to_ytm",
    )

    assert len(matched) == 1
    assert unmatched == []
    loaded = cache.get_match("spotify_to_ytm", wanted[0])
    assert loaded is not None
    assert loaded.source_id == "yt1"
