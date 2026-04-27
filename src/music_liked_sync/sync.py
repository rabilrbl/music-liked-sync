import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from .cache import SyncCache
from .constants import DEFAULT_BATCH_DELAY, DEFAULT_BATCH_SIZE
from .models import Track
from .utils import (
    batched,
    best_match,
    normalize_artist,
    normalize_key,
    sleep_between_batches,
)


def compute_missing(left: Sequence[Track], right: Sequence[Track], verbose: bool = False) -> list[Track]:
    right_keys = {normalize_key(track.title, track.artists) for track in right}

    # Pre-index right library by normalized artist for faster fuzzy lookups
    right_by_artist: dict[str, list[Track]] = {}
    for track in right:
        for artist in track.artists:
            norm_a = normalize_artist(artist)
            if norm_a:
                right_by_artist.setdefault(norm_a, []).append(track)

    missing = []
    for i, track in enumerate(left):
        if verbose and i % 100 == 0 and i > 0:
            print(f"  Comparing track {i}/{len(left)}...", end="\r", flush=True)

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
        print("".ljust(90), end="\r")
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

    def vprint(*msg):
        if verbose:
            with matched_lock:
                # Clear progress line before printing verbose log
                print("".ljust(90), end="\r")
                print(*msg)

    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    vprint(f"Resolving matches for {len(candidates_to_process)} tracks ({label})...")
    chunks = batched(candidates_to_process, batch_size)

    def process_track(wanted: Track) -> tuple[Track, Track | None, bool]:
        """Returns (wanted, match, failed_search)"""
        cached_match = None
        if cache and cache_direction and cache_read:
            cached_match = cache.get_match(cache_direction, wanted)
        if cached_match:
            vprint(f"  [CACHE] {wanted.display} -> {cached_match.display}")
            return wanted, cached_match, False

        try:
            candidates = search_fn(wanted)
        except RuntimeError:
            raise
        except Exception as exc:
            summary = str(exc).strip().splitlines()[0][:180] if str(exc).strip() else exc.__class__.__name__
            return wanted, None, True, summary  # type: ignore

        match = best_match(wanted, candidates)
        if match:
            vprint(f"  [MATCH] {wanted.display} -> {match.display}")
            if cache and cache_direction and cache_write:
                cache.store_match(cache_direction, wanted, match)
        else:
            vprint(f"  [MISS]  {wanted.display} (no match in {len(candidates)} search results)")
        
        return wanted, match, False

    def update_progress():
        with matched_lock:
            print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for batch_index, chunk in enumerate(chunks):
            futures = [executor.submit(process_track, wanted) for wanted in chunk]
            for future in futures:
                try:
                    res = future.result()
                    if len(res) == 4:  # Failure case
                        wanted, _, _, summary = res
                        print(f"\n{label}: search failed for {wanted.display}; treating as unresolved ({summary})", file=sys.stderr)
                        with matched_lock:
                            unmatched.append(wanted)
                    else:
                        wanted, match, _ = res
                        with matched_lock:
                            if match:
                                matched.append((wanted, match))
                            else:
                                unmatched.append(wanted)
                    update_progress()
                except RuntimeError:
                    # Cancel pending futures and re-raise
                    for f in futures:
                        f.cancel()
                    raise

            sleep_between_batches(batch_index, len(chunks), batch_delay, sleep_fn)

    print("".ljust(90), end="\r")
    return matched, unmatched
