import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cache import SyncCache
from .constants import DEFAULT_BATCH_DELAY, DEFAULT_BATCH_SIZE
from .models import Track
from .sync import compute_missing, resolve_matches


@dataclass
class SyncPipeline:
    """Orchestrates a full sync run between Spotify and YouTube Music."""

    spotify: Any  # SpotifyBackend
    ytm: Any  # YTMusicBackend
    cache: SyncCache

    # Sync options
    spotify_to_ytm: bool = True
    ytm_to_spotify: bool = True
    apply: bool = False
    max_add: int | None = None
    batch_size: int = DEFAULT_BATCH_SIZE
    batch_delay: float = DEFAULT_BATCH_DELAY
    workers: int = 4
    verbose: bool = False
    cache_read: bool = True
    cache_write: bool = True
    cache_library_ttl: float = 0.0
    report_path: Path | None = None

    # Collected results
    report: dict[str, Any] = field(default_factory=dict, init=False)

    def run(self) -> int:
        """Run the sync. Returns 0 on success, 2 on fatal error."""
        self.report = {
            "apply": self.apply,
            "spotify_liked_count": 0,
            "ytm_liked_count": 0,
            "report": str(self.report_path) if self.report_path else None,
            "batch_size": self.batch_size,
            "batch_delay": self.batch_delay,
            "max_add": self.max_add,
            "yt_auth": getattr(self.ytm, "mode", "browser-session"),
            "spotify_auth": getattr(self.spotify, "mode", "web-session"),
            "cache_db": str(self.cache.path),
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cache_library_ttl": self.cache_library_ttl,
        }

        try:
            spotify_liked = self.spotify.liked_tracks(
                max_workers=self.workers, verbose=self.verbose
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        self.report["spotify_liked_count"] = len(spotify_liked)

        try:
            ytm_liked = self.ytm.liked_tracks(verbose=self.verbose)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        self.report["ytm_liked_count"] = len(ytm_liked)

        if self.cache_write and self.cache_library_ttl > 0:
            self.cache.store_library("spotify", spotify_liked)
            self.cache.store_library("ytm", ytm_liked)

        if self.cache_read and self.cache_library_ttl > 0:
            cached_spotify = self.cache.get_library("spotify", max_age_seconds=self.cache_library_ttl)
            if cached_spotify:
                spotify_liked = cached_spotify
            cached_ytm = self.cache.get_library("ytm", max_age_seconds=self.cache_library_ttl)
            if cached_ytm:
                ytm_liked = cached_ytm

        if self.spotify_to_ytm:
            status = self._sync_direction(
                source=spotify_liked,
                target_library=ytm_liked,
                search_fn=self.ytm.search_track,
                label="Spotify → YTM",
                direction_key="spotify_to_ytm",
                like_fn=lambda tracks: self.ytm.like_tracks(
                    tracks, batch_size=self.batch_size, batch_delay=self.batch_delay,
                    max_workers=self.workers, verbose=self.verbose,
                ),
                like_service="ytm",
            )
            if status != 0:
                return status

        if self.ytm_to_spotify:
            status = self._sync_direction(
                source=ytm_liked,
                target_library=spotify_liked,
                search_fn=self.spotify.search_track,
                label="YTM → Spotify",
                direction_key="ytm_to_spotify",
                like_fn=lambda tracks: self.spotify.save_tracks(
                    tracks, batch_size=self.batch_size, batch_delay=self.batch_delay,
                    max_workers=self.workers, verbose=self.verbose,
                ),
                like_service="spotify",
            )
            if status != 0:
                return status

        if self.report_path:
            self.report_path.write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        print(json.dumps({
            "apply": self.apply,
            "spotify_liked_count": self.report["spotify_liked_count"],
            "ytm_liked_count": self.report["ytm_liked_count"],
            "report": str(self.report_path) if self.report_path else None,
            "batch_size": self.batch_size,
            "batch_delay": self.batch_delay,
            "max_add": self.max_add,
            "yt_auth": getattr(self.ytm, "mode", "browser-session"),
            "spotify_auth": getattr(self.spotify, "mode", "web-session"),
            "cache_db": str(self.cache.path),
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cache_library_ttl": self.cache_library_ttl,
        }, ensure_ascii=False, indent=2))
        return 0

    def _sync_direction(
        self,
        *,
        source: list[Track],
        target_library: list[Track],
        search_fn,
        label: str,
        direction_key: str,
        like_fn,
        like_service: str,
    ) -> int:
        """Sync tracks from source to target direction. Returns 0 on success, 2 on fatal error."""
        missing = compute_missing(source, target_library, verbose=self.verbose)
        if self.verbose:
            print(f"{label}: {len(missing)} tracks missing in target", file=sys.stderr)

        try:
            matched, unmatched = resolve_matches(
                missing,
                search_fn,
                self.max_add,
                label,
                batch_size=self.batch_size,
                batch_delay=self.batch_delay,
                cache=self.cache,
                cache_direction=direction_key,
                cache_read=self.cache_read,
                cache_write=self.cache_write,
                max_workers=self.workers,
                verbose=self.verbose,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

        if self.apply:
            to_like = [match for _, match in matched]
            if self.cache_read:
                to_like = [
                    track for track in to_like
                    if not self.cache.is_liked(like_service, track.source_id)
                ]
            if self.verbose:
                print(f"{label}: Liking {len(to_like)} tracks", file=sys.stderr)
            try:
                like_fn(to_like)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 2
            if self.cache_write:
                self.cache.mark_liked_many(like_service, [t.source_id for t in to_like])

        self.report[direction_key] = {
            "missing_count": len(missing),
            "matched_count": len(matched),
            "unmatched_count_sampled": len(unmatched),
            "matched": [
                {"source": _track_dict(src), "target": _track_dict(dst)}
                for src, dst in matched
            ],
            "unmatched": [_track_dict(track) for track in unmatched],
        }
        return 0


def _track_dict(track: Track) -> dict[str, Any]:
    return {
        "title": track.title,
        "artists": list(track.artists),
        "source_id": track.source_id,
        "duration_ms": track.duration_ms,
        "album": track.album,
    }
