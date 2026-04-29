import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from .cache import SyncCache
from .constants import DEFAULT_BATCH_DELAY, DEFAULT_BATCH_SIZE
from .models import SearchResult, Track
from .utils import (
    batched,
    best_match,
    normalize_artist,
    normalize_key,
    sleep_between_batches,
)

_PROGRESS_WIDTH = 90


class _Progress:
    """Thread-safe progress reporter that uses stderr for status lines."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Clear the current progress line."""
        sys.stderr.write("\r" + " " * _PROGRESS_WIDTH + "\r")
        sys.stderr.flush()

    def status(self, message: str) -> None:
        """Write an inline status line (overwrites previous), padded to clear stale chars."""
        with self._lock:
            padded = message[:_PROGRESS_WIDTH].ljust(_PROGRESS_WIDTH)
            sys.stderr.write(f"\r{padded}")
            sys.stderr.flush()

    def log(self, *msg) -> None:
        """Write a verbose log line to stderr, clearing the progress line first."""
        if not self.verbose:
            return
        with self._lock:
            self.clear()
            print(*msg, file=sys.stderr)

    def finalize(self) -> None:
        """Clear the progress line after work is done."""
        with self._lock:
            self.clear()


def compute_missing(left: Sequence[Track], right: Sequence[Track], verbose: bool = False) -> list[Track]:
    right_keys = {normalize_key(track.title, track.artists) for track in right}

    # Pre-index right library by normalized artist for faster fuzzy lookups
    right_by_artist: dict[str, list[Track]] = {}
    for track in right:
        for artist in track.artists:
            norm_a = normalize_artist(artist)
            if norm_a:
                right_by_artist.setdefault(norm_a, []).append(track)

    progress = _Progress(verbose)
    missing = []
    for i, track in enumerate(left):
        if verbose and i % 100 == 0 and i > 0:
            progress.status(f"  Comparing track {i}/{len(left)}...")

        key = normalize_key(track.title, track.artists)
        if key in right_keys:
            continue

        # Fallback: fuzzy check only against tracks with at least one matching artist
        possible_candidates = []
        seen_ids = set()
        for artist in track.artists:
            norm_a = normalize_artist(artist)
            for cand in right_by_artist.get(norm_a, []):
                if cand.source_id not in seen_ids:
                    possible_candidates.append(cand)
                    seen_ids.add(cand.source_id)

        if possible_candidates and best_match(track, possible_candidates):
            continue

        missing.append(track)

    if verbose:
        progress.finalize()
    return missing


def resolve_matches(
    missing: Sequence[Track],
    search_fn: Callable[[Track], list[Track]],
    max_add: int | None,
    label: str,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    batch_delay: float = DEFAULT_BATCH_DELAY,
    sleep_fn: Callable[[float], None] = time.sleep,
    cache: SyncCache | None = None,
    cache_direction: str | None = None,
    cache_read: bool = True,
    cache_write: bool = True,
    max_workers: int = 4,
    verbose: bool = False,
) -> tuple[list[tuple[Track, Track]], list[Track]]:
    matched: list[tuple[Track, Track]] = []
    unmatched: list[Track] = []
    matched_lock = threading.Lock()
    progress = _Progress(verbose)

    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    progress.log(f"Resolving matches for {len(candidates_to_process)} tracks ({label})...")
    chunks = batched(candidates_to_process, batch_size)

    def process_track(wanted: Track) -> SearchResult:
        """Search for a track match, using cache if available."""
        cached_match = None
        if cache and cache_direction and cache_read:
            cached_match = cache.get_match(cache_direction, wanted)
        if cached_match:
            progress.log(f"  [CACHE] {wanted.display} -> {cached_match.display}")
            return SearchResult(wanted=wanted, match=cached_match, search_failed=False)

        try:
            candidates = search_fn(wanted)
        except RuntimeError:
            raise
        except Exception as exc:
            summary = str(exc).strip().splitlines()[0][:180] if str(exc).strip() else exc.__class__.__name__
            return SearchResult(wanted=wanted, match=None, search_failed=True, error_summary=summary)

        match = best_match(wanted, candidates)
        if match:
            progress.log(f"  [MATCH] {wanted.display} -> {match.display}")
            if cache and cache_direction and cache_write:
                cache.store_match(cache_direction, wanted, match)
        else:
            progress.log(f"  [MISS]  {wanted.display} (no match in {len(candidates)} search results)")
        
        return SearchResult(wanted=wanted, match=match, search_failed=False)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            for batch_index, chunk in enumerate(chunks):
                futures = [executor.submit(process_track, wanted) for wanted in chunk]
                for future in futures:
                    try:
                        res = future.result()
                        if res.search_failed:
                            progress.log(f"{label}: search failed for {res.wanted.display}; treating as unresolved ({res.error_summary})")
                            with matched_lock:
                                unmatched.append(res.wanted)
                                n_matched = len(matched)
                                n_unmatched = len(unmatched)
                        else:
                            with matched_lock:
                                if res.match:
                                    matched.append((res.wanted, res.match))
                                else:
                                    unmatched.append(res.wanted)
                                n_matched = len(matched)
                                n_unmatched = len(unmatched)
                        progress.status(f"{label}: {n_matched} matched, {n_unmatched} unmatched")
                    except RuntimeError:
                        # Cancel pending futures and re-raise
                        for f in futures:
                            f.cancel()
                        raise

                sleep_between_batches(batch_index, len(chunks), batch_delay, sleep_fn)
        finally:
            progress.finalize()
    return matched, unmatched
