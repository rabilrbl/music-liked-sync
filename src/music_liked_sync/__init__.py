"""Bidirectional sync for Spotify and YouTube Music liked songs."""

__version__ = "0.1.1"

from .models import Track as Track
from .spotify import SpotifyBackend as SpotifyBackend
from .ytmusic import YTMusicBackend as YTMusicBackend
from .cache import SyncCache as SyncCache
from .cli import main as main
