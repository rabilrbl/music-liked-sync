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
    normalize_key,
    sleep_between_batches,
)


def compute_missing(left: Sequence[Track], right: Sequence[Track]) -> list[Track]:
    right_keys = {normalize_key(track.title, track.artists) for track in right}
    missing = []
    for track in left:
        key = normalize_key(track.title, track.artists)
        if key in right_keys:
            continue
        # Fallback: fuzzy check against the whole library for nearly identical tracks
        if best_match(track, right):
            continue
        missing.append(track)
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
) -> tuple[list[tuple[Track, Track]], list[Track]]:
    matched: list[tuple[Track, Track]] = []
    unmatched: list[Track] = []
    matched_lock = threading.Lock()

    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    chunks = batched(candidates_to_process, batch_size)

    def process_track(wanted: Track) -> tuple[Track, Track | None, bool]:
        """Returns (wanted, match, failed_search)"""
        cached_match = None
        if cache and cache_direction and cache_read:
            cached_match = cache.get_match(cache_direction, wanted)
        if cached_match:
            return wanted, cached_match, False

        try:
            candidates = search_fn(wanted)
        except RuntimeError:
            raise
        except Exception as exc:
            summary = str(exc).strip().splitlines()[0][:180] if str(exc).strip() else exc.__class__.__name__
            return wanted, None, True, summary  # type: ignore

        match = best_match(wanted, candidates)
        if match and cache and cache_direction and cache_write:
            cache.store_match(cache_direction, wanted, match)
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
