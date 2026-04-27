from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Track:
    title: str
    artists: tuple[str, ...]
    source_id: str
    duration_ms: int | None = None
    album: str | None = None

    @property
    def display(self) -> str:
        artist = ", ".join(self.artists) if self.artists else "Unknown Artist"
        return f"{self.title} — {artist}"


@dataclass(frozen=True)
class SpotifyWebSessionState:
    access_token: str
    user_agent: str
    client_token: str | None = None
    app_version: str | None = None
