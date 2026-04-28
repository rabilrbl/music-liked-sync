package music_liked_sync

import "regexp"

var CommonTitleSuffixRE = regexp.MustCompile(`(?i)\s*(?:[-–—:]\s*)?\(?\b(?:remaster(?:ed)?(?:\s*\d{2,4})?|\d{4}\s*remaster(?:ed)?|deluxe(?:\s+edition)?|expanded(?:\s+edition)?|explicit|clean|single version|album version|radio edit|edit|live|mono|stereo|from .*|official audio|official video|official music video|official lyric video|lyric video|lyrics|audio only|video only|music video|full video|music audio|high quality|hq|hd|topic|original motion picture soundtrack|soundtrack|ost)\b\)?\s*$`)

const (
	DefaultMarket                 = "IN"
	DefaultBatchSize              = 50
	DefaultBatchDelay             = 1.0
	DefaultCacheDB                = "state/sync-cache.sqlite3"
	DefaultLibraryCacheTTL        = 0.0
	DefaultSpotifyWebSessionDir   = "auth/spotify-web-session"
	DefaultSpotifyWebLockFile     = "state/locks/spotify-web-session.lock"
	DefaultSpotifyWebLoginTimeout = 300.0
	SpotifyWebOrigin              = "https://open.spotify.com"
	SpotifyWebTokenURLPrefix      = "https://open.spotify.com/api/token"
	SpotifyWebPathfinderURL       = "https://api-partner.spotify.com/pathfinder/v2/query"
	SpotifyWebRequiredCookie      = "sp_dc"
	SpotifyAPIBase                = "https://api.spotify.com/v1"
	DefaultBrowserUserAgent       = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

	DefaultYTBrowserSessionDir   = "auth/ytmusic-browser-session"
	DefaultYTBrowserLockFile     = "state/locks/ytmusic-browser-session.lock"
	YTMusicOrigin                = "https://music.youtube.com"
	YTMusicRequiredCookie        = "__Secure-3PAPISID"
	DefaultYTBrowserLoginTimeout = 300.0

	SpotifyRetryAttempts  = 5
	SpotifyRetryBaseDelay = 2.0
	SpotifyMaxRetryAfter  = 30.0
	YTMRetryAttempts      = 4
	YTMRetryBaseDelay     = 2.0
)

var ArtistSplitRE = regexp.MustCompile(`(?i)\s*(?:,|/|&| x | and | feat\.? | ft\.? | featuring )\s*`)
