import threading

from music_liked_sync.cache import SyncCache
from music_liked_sync.models import Track


def test_sync_cache_roundtrip_store_lookup_and_mark_liked(tmp_path):
    cache = SyncCache(tmp_path / "sync-cache.sqlite3")
    source = Track(
        title="Believer", artists=("Imagine Dragons",), source_id="spotify:track:1"
    )
    target = Track(title="Believer", artists=("Imagine Dragons",), source_id="yt1")

    assert cache.get_match("spotify_to_ytm", source) is None

    cache.store_match("spotify_to_ytm", source, target)
    loaded = cache.get_match("spotify_to_ytm", source)
    assert loaded is not None
    assert loaded.source_id == "yt1"

    assert not cache.is_liked("ytm", "yt1")
    cache.mark_liked("ytm", "yt1")
    assert cache.is_liked("ytm", "yt1")


def test_mark_liked_many_empty(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    cache.mark_liked_many("ytm", [])  # Should not crash


def test_get_match_deserialization_error(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    source = Track(title="S", artists=("A",), source_id="1")
    cache.store_match("d", source, source)

    # Corrupt the data using the persistent connection
    cache._conn.execute("UPDATE matches SET target_track_json = 'not json'")
    cache._conn.commit()

    assert cache.get_match("d", source) is None


def test_library_cache_roundtrip_and_expiration(tmp_path, monkeypatch):
    cache = SyncCache(tmp_path / "cache.db")
    tracks = [Track(title="S", artists=("A",), source_id="1")]

    assert cache.get_library("s", 3600) is None

    cache.store_library("s", tracks)
    loaded = cache.get_library("s", 3600)
    assert len(loaded) == 1
    assert loaded[0].title == "S"

    assert cache.get_library("s", -1) is None

    # Expiration
    import time
    original_time = time.time
    monkeypatch.setattr(time, "time", lambda: original_time() + 4000)
    assert cache.get_library("s", 3600) is None


def test_get_library_deserialization_error(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    cache.store_library("s", [Track(title="S", artists=("A",), source_id="1")])

    cache._conn.execute("UPDATE library_cache SET tracks_json = 'not a list'")
    cache._conn.commit()
    assert cache.get_library("s", 3600) is None

    cache._conn.execute("UPDATE library_cache SET tracks_json = '\"not a list\"'")
    cache._conn.commit()
    assert cache.get_library("s", 3600) is None


def test_concurrent_read_write(tmp_path):
    """Regression test: concurrent reads+writes must not raise InterfaceError."""
    cache = SyncCache(tmp_path / "cache.db")
    source = Track(title="S", artists=("A",), source_id="1")

    errors: list[Exception] = []

    def writer():
        try:
            for i in range(50):
                t = Track(title=f"S{i}", artists=("A",), source_id=str(i))
                cache.store_match("dir", t, t)
                cache.mark_liked("svc", str(i))
        except Exception as exc:
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                cache.get_match("dir", source)
                cache.is_liked("svc", "1")
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent access errors: {errors}"


def test_close_idempotent(tmp_path):
    """close() can be called multiple times without error."""
    cache = SyncCache(tmp_path / "cache.db")
    cache.close()
    cache.close()  # second close must not raise


def test_mark_liked_many_with_data(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    ids = ["id3", "id1", "id2"]
    cache.mark_liked_many("ytm", ids)
    assert cache.is_liked("ytm", "id1")
    assert cache.is_liked("ytm", "id2")
    assert cache.is_liked("ytm", "id3")


def test_mark_liked_many_deduplicates(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    cache.mark_liked_many("ytm", ["a", "a", "b"])
    assert cache.is_liked("ytm", "a")
    assert cache.is_liked("ytm", "b")


def test_mark_liked_many_filters_empty_strings(tmp_path):
    cache = SyncCache(tmp_path / "cache.db")
    cache.mark_liked_many("ytm", ["", "valid", ""])
    assert not cache.is_liked("ytm", "")
    assert cache.is_liked("ytm", "valid")
