import sys
import time
from collections.abc import Callable, Sequence

from .cache import SyncCache
from .constants import DEFAULT_BATCH_DELAY, DEFAULT_BATCH_SIZE
from .models import Track
from .utils import (
    batched,
    best_match,
    normalize_key,
    sleep_between_batches,
    unique_by_key,
)


def compute_missing(left: Sequence[Track], right: Sequence[Track]) -> list[Track]:
    right_keys = set(unique_by_key(right))
    return [track for track in left if normalize_key(track.title, track.artists) not in right_keys]


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
) -> tuple[list[tuple[Track, Track]], list[Track]]:
    matched: list[tuple[Track, Track]] = []
    unmatched: list[Track] = []
    candidates_to_process = list(missing if max_add is None else missing[:max_add])
    chunks = batched(candidates_to_process, batch_size)
    for batch_index, chunk in enumerate(chunks):
        for wanted in chunk:
            cached_match = None
            if cache and cache_direction and cache_read:
                cached_match = cache.get_match(cache_direction, wanted)
            if cached_match:
                matched.append((wanted, cached_match))
                print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
                continue
            try:
                candidates = search_fn(wanted)
            except RuntimeError:
                raise
            except Exception as exc:
                summary = str(exc).strip().splitlines()[0][:180] if str(exc).strip() else exc.__class__.__name__
                print(f"\n{label}: search failed for {wanted.display}; treating as unresolved ({summary})", file=sys.stderr)
                unmatched.append(wanted)
                print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
                continue
            match = best_match(wanted, candidates)
            if match:
                matched.append((wanted, match))
                if cache and cache_direction and cache_write:
                    cache.store_match(cache_direction, wanted, match)
            else:
                unmatched.append(wanted)
            print(f"{label}: {len(matched)} matched, {len(unmatched)} unmatched", end="\r", flush=True)
        sleep_between_batches(batch_index, len(chunks), batch_delay, sleep_fn)
    print("".ljust(90), end="\r")
    return matched, unmatched
