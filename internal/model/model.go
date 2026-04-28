package model

import (
	"fmt"
	"strings"
)

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

type MatchedTrack struct {
	Source Track   `json:"source"`
	Target Track   `json:"target"`
	Score  float64 `json:"score"`
}
