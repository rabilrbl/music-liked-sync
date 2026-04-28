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
