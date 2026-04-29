from music_liked_sync.sync import resolve_matches, compute_missing, _Progress
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


# --- _Progress ---

def test_progress_status(capsys):
    p = _Progress(verbose=False)
    p.status("Working on 5/100...")
    captured = capsys.readouterr()
    assert "Working on 5/100" in captured.err


def test_progress_log_verbose(capsys):
    p = _Progress(verbose=True)
    p.log("Detail message")
    captured = capsys.readouterr()
    assert "Detail message" in captured.err


def test_progress_log_not_verbose(capsys):
    p = _Progress(verbose=False)
    p.log("Should not appear")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_progress_finalize(capsys):
    p = _Progress(verbose=False)
    p.status("Working...")
    p.finalize()
    captured = capsys.readouterr()
    # finalize clears the line
    assert "\r" in captured.err


# --- compute_missing edge cases ---

def test_compute_missing_empty_input():
    t1 = Track(title="Song", artists=("A",), source_id="1")
    assert compute_missing([], [t1]) == []
    assert compute_missing([t1], []) == [t1]
    assert compute_missing([], []) == []


def test_compute_missing_all_match():
    t1 = Track(title="Song", artists=("A",), source_id="1")
    assert compute_missing([t1], [t1]) == []


# --- resolve_matches with max_add ---

def test_resolve_matches_respects_max_add(tmp_path):
    tracks = [
        Track(title=f"Song {i}", artists=("Artist",), source_id=f"yt{i}") for i in range(5)
    ]

    def search_fn(wanted):
        return [Track(title=wanted.title, artists=("Artist",), source_id=f"match-{wanted.source_id}")]

    matched, unmatched = resolve_matches(
        tracks, search_fn, max_add=2, label="test", batch_delay=0
    )
    assert len(matched) + len(unmatched) == 2


def test_resolve_matches_cache_read_false(tmp_path):
    wanted = Track(title="Believer", artists=("Imagine Dragons",), source_id="1")
    cached = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")
    cache = SyncCache(tmp_path / "cache.db")
    cache.store_match("d", wanted, cached)

    calls = {"search": 0}
    def search_fn(_track):
        calls["search"] += 1
        return []

    matched, _ = resolve_matches(
        [wanted], search_fn, None, "test", cache=cache, cache_direction="d", cache_read=False
    )
    assert calls["search"] == 1
    assert matched == []
