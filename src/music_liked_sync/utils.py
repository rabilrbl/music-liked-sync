import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from difflib import SequenceMatcher
from pathlib import Path

from .constants import ARTIST_SPLIT_RE, COMMON_TITLE_SUFFIX_RE
from .models import Track


def _ascii_lower(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return value.lower()


def normalize_text(value: str, artists: Sequence[str] | None = None) -> str:
    text = _ascii_lower(value)
    text = text.replace("&", " and ")
    
    # Handle common abbreviations
    text = re.sub(r"\bu\b", "you", text)
    text = re.sub(r"\br\b", "are", text)
    text = re.sub(r"\bw/\b", "with", text)
    text = re.sub(r"\bw/o\b", "without", text)
    
    # Aggressively strip metadata after common YTM delimiters
    # e.g., "Song Name | Season 14 | Pasoori" -> "Song Name Pasoori"
    # Actually, often it's "Coke Studio | Season 14 | Pasoori"
    text = re.sub(r"\bcoke studio\s*\|\s*season\s*\d+\s*\|\s*", "", text)
    
    # If it's "Artist - Song", and "Artist" is in our artists list, strip it
    if artists:
        for artist in artists:
            norm_a = _ascii_lower(artist)
            # Match "Artist - " or "Artist: " at the start
            text = re.sub(f"^{re.escape(norm_a)}\\s*[-–—:]\\s*", "", text)
            # Also handle "Artist | "
            text = re.sub(f"^{re.escape(norm_a)}\\s*\\|\\s*", "", text)

    # Remove metadata after certain delimiters if they appear near the end or look like noise
    # | is very common for "Song | Metadata"
    text = re.sub(r"\s*\|\s*.*$", "", text)
    
    # Remove featuring artist parts from title
    text = re.sub(r"\s+\(?(?:feat|ft|featuring)\.?\s+.*$", "", text)
    
    previous = None
    while previous != text:
        previous = text
        text = COMMON_TITLE_SUFFIX_RE.sub("", text)
    
    # Remove any remaining content in parentheses/brackets that often contains metadata
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", text)
    
    # Remove everything except alphanumeric and spaces
    text = re.sub(r"[^a-z0-9\s]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_artist(value: str) -> str:
    text = normalize_text(value)
    # Artist names often differ only by spacing.
    return text.replace(" ", "")


def normalize_key(title: str, artists: Sequence[str]) -> str:
    artist_part = "+".join(sorted(normalize_artist(a) for a in artists if a))
    return f"{normalize_text(title, artists)}::{artist_part}"


def artist_matches(left: Sequence[str], right: Sequence[str]) -> bool:
    left_norm = [normalize_artist(a) for a in left if a]
    right_norm = [normalize_artist(a) for a in right if a]
    if not left_norm or not right_norm:
        return False
    for left_artist in left_norm:
        for right_artist in right_norm:
            if left_artist == right_artist or left_artist in right_artist or right_artist in left_artist:
                return True
            if SequenceMatcher(None, left_artist, right_artist).ratio() >= 0.86:
                return True
    return False


def track_similarity(wanted: Track, candidate: Track) -> float:
    title_score = SequenceMatcher(
        None, normalize_text(wanted.title, wanted.artists), normalize_text(candidate.title, candidate.artists)
    ).ratio()
    artist_score = 1.0 if artist_matches(wanted.artists, candidate.artists) else 0.0
    duration_score = 0.0
    if wanted.duration_ms and candidate.duration_ms:
        delta = abs(wanted.duration_ms - candidate.duration_ms)
        duration_score = max(0.0, 1.0 - delta / 30000)  # full credit within ~0s, none after 30s
    return (title_score * 0.62) + (artist_score * 0.33) + (duration_score * 0.05)


def best_match(wanted: Track, candidates: Sequence[Track], threshold: float = 0.82) -> Track | None:
    if not candidates:
        return None
    wanted_key = normalize_key(wanted.title, wanted.artists)
    for candidate in candidates:
        if normalize_key(candidate.title, candidate.artists) == wanted_key:
            return candidate
    scored = sorted(((track_similarity(wanted, c), c) for c in candidates), key=lambda item: item[0], reverse=True)
    score, candidate = scored[0]
    if score >= threshold and artist_matches(wanted.artists, candidate.artists):
        return candidate
    return None


def primary_search_artist(artists: Sequence[str]) -> str:
    for artist in artists:
        for part in ARTIST_SPLIT_RE.split(artist):
            cleaned = part.strip(" -")
            if cleaned:
                return cleaned
    return ""


def truncate_query(query: str, limit: int = 240) -> str:
    query = re.sub(r"\s+", " ", query).strip()
    if len(query) <= limit:
        return query
    truncated = query[:limit].rsplit(" ", 1)[0].strip()
    return truncated or query[:limit]


def unique_by_key(tracks: Iterable[Track]) -> dict[str, Track]:
    out: dict[str, Track] = {}
    for track in tracks:
        out.setdefault(normalize_key(track.title, track.artists), track)
    return out


def batched(items: Sequence, batch_size: int) -> list[Sequence]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [items[start : start + batch_size] for start in range(0, len(items), batch_size)]


def sleep_between_batches(
    batch_index: int,
    total_batches: int,
    batch_delay: float,
    sleep_fn: Callable[[float], None],
) -> None:
    if batch_delay > 0 and batch_index < total_batches - 1:
        sleep_fn(batch_delay)


def read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
