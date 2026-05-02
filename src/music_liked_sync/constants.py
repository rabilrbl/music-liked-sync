import re

COMMON_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:[-–—:]\s*)?\(?\b(?:remaster(?:ed)?(?:\s*\d{2,4})?|\d{4}\s*remaster(?:ed)?|"
    r"deluxe(?:\s+edition)?|expanded(?:\s+edition)?|explicit|clean|single version|album version|"
    r"radio edit|edit|live|mono|stereo|from .*|official audio|official video|official music video|"
    r"official lyric video|lyric video|lyrics|audio only|video only|music video|full video|"
    r"music audio|high quality|hq|hd|topic|original motion picture soundtrack|soundtrack|ost)\b\)?\s*$",
    re.IGNORECASE,
)
DEFAULT_MARKET = "IN"
DEFAULT_BATCH_SIZE = 50
DEFAULT_BATCH_DELAY = 1.0
DEFAULT_CACHE_DB = "state/sync-cache.sqlite3"
DEFAULT_LIBRARY_CACHE_TTL = 0.0
DEFAULT_SPOTIFY_WEB_SESSION_DIR = "auth/spotify-web-session"
DEFAULT_SPOTIFY_WEB_LOCK_FILE = "state/locks/spotify-web-session.lock"
DEFAULT_SPOTIFY_WEB_LOGIN_TIMEOUT = 300.0
SPOTIFY_WEB_ORIGIN = "https://open.spotify.com"
SPOTIFY_WEB_TOKEN_URL_PREFIX = f"{SPOTIFY_WEB_ORIGIN}/api/token"
SPOTIFY_WEB_PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
SPOTIFY_WEB_REQUIRED_COOKIE = "sp_dc"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
DEFAULT_BROWSER_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

DEFAULT_YT_BROWSER_SESSION_DIR = "auth/ytmusic-browser-session"
DEFAULT_YT_BROWSER_LOCK_FILE = "state/locks/ytmusic-browser-session.lock"
YTMUSIC_ORIGIN = "https://music.youtube.com"
YTMUSIC_REQUIRED_COOKIE = "__Secure-3PAPISID"
DEFAULT_YT_BROWSER_LOGIN_TIMEOUT = 300.0

SPOTIFY_RETRY_ATTEMPTS = 5
SPOTIFY_RETRY_BASE_DELAY = 2.0
SPOTIFY_MAX_RETRY_AFTER = 30.0
YTM_RETRY_ATTEMPTS = 4
YTM_RETRY_BASE_DELAY = 2.0
ARTIST_SPLIT_RE = re.compile(r"\s*(?:,|/|&| x | and | feat\.? | ft\.? | featuring )\s*", re.IGNORECASE)

# Spotify GraphQL persisted query hashes — overridable via env for rotation events
SPOTIFY_LIBRARY_QUERY_HASH = "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240"
SPOTIFY_SEARCH_QUERY_HASH = "75a88491b7c54a02065a24d6e836121ab20ca42d1bede25a0e06fe5018033ffe"
SPOTIFY_SAVE_QUERY_HASH = "7c5a69420e2bfae3da5cc4e14cbc8bb3f6090f80afc00ffc179177f19be3f33d"
