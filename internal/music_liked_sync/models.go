package music_liked_sync

import "fmt"
import "strings"

type Track struct {
	Title      string   `json:"title"`
	Artists    []string `json:"artists"`
	SourceID   string   `json:"source_id"`
	DurationMs *int     `json:"duration_ms"`
	Album      *string  `json:"album"`
}

func (t Track) Display() string {
	artist := "Unknown Artist"
	if len(t.Artists) > 0 {
		artist = strings.Join(t.Artists, ", ")
	}
	return fmt.Sprintf("%s — %s", t.Title, artist)
}

type SpotifyWebSessionState struct {
	AccessToken string  `json:"access_token"`
	UserAgent   string  `json:"user_agent"`
	ClientToken *string `json:"client_token"`
	AppVersion  *string `json:"app_version"`
}
