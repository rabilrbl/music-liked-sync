import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from .models import Track
from .utils import normalize_key


class SyncCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS matches (
                    direction TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_track_json TEXT NOT NULL,
                    target_track_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (direction, source_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS liked_tracks (
                    service TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (service, source_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS library_cache (
                    service TEXT NOT NULL PRIMARY KEY,
                    tracks_json TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _serialize_track(track: Track) -> str:
        return json.dumps(asdict(track), ensure_ascii=False)

    @staticmethod
    def _deserialize_track(payload: str) -> Track:
        data = json.loads(payload)
        return Track(
            title=str(data.get("title") or ""),
            artists=tuple(str(artist) for artist in (data.get("artists") or [])),
            source_id=str(data.get("source_id") or ""),
            duration_ms=data.get("duration_ms"),
            album=data.get("album"),
        )

    def store_match(self, direction: str, source: Track, target: Track) -> None:
        source_key = normalize_key(source.title, source.artists)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO matches(direction, source_key, source_track_json, target_track_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(direction, source_key) DO UPDATE SET
                    source_track_json=excluded.source_track_json,
                    target_track_json=excluded.target_track_json,
                    updated_at=excluded.updated_at
                """,
                (
                    direction,
                    source_key,
                    self._serialize_track(source),
                    self._serialize_track(target),
                    now,
                ),
            )
            conn.commit()

    def get_match(self, direction: str, source: Track) -> Track | None:
        source_key = normalize_key(source.title, source.artists)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT target_track_json FROM matches WHERE direction = ? AND source_key = ?",
                (direction, source_key),
            ).fetchone()
        if not row:
            return None
        try:
            return self._deserialize_track(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def mark_liked(self, service: str, source_id: str) -> None:
        self.mark_liked_many(service, [source_id])

    def mark_liked_many(self, service: str, source_ids: Sequence[str]) -> None:
        now = time.time()
        rows = [(service, source_id, now) for source_id in sorted(set(source_ids)) if source_id]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO liked_tracks(service, source_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service, source_id) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            conn.commit()

    def is_liked(self, service: str, source_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM liked_tracks WHERE service = ? AND source_id = ? LIMIT 1",
                (service, source_id),
            ).fetchone()
        return row is not None

    def store_library(self, service: str, tracks: Sequence[Track]) -> None:
        payload = json.dumps([asdict(track) for track in tracks], ensure_ascii=False)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO library_cache(service, tracks_json, fetched_at)
                VALUES (?, ?, ?)
                ON CONFLICT(service) DO UPDATE SET
                    tracks_json=excluded.tracks_json,
                    fetched_at=excluded.fetched_at
                """,
                (service, payload, now),
            )
            conn.commit()

    def get_library(self, service: str, max_age_seconds: float) -> list[Track] | None:
        if max_age_seconds <= 0:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tracks_json, fetched_at FROM library_cache WHERE service = ?",
                (service,),
            ).fetchone()
        if not row:
            return None
        tracks_json, fetched_at = row
        if (time.time() - float(fetched_at)) > max_age_seconds:
            return None
        try:
            raw_tracks = json.loads(tracks_json)
            if not isinstance(raw_tracks, list):
                return None
            return [self._deserialize_track(json.dumps(item, ensure_ascii=False)) for item in raw_tracks]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
