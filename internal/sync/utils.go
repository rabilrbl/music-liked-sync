package sync

import (
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"unicode"

	"github.com/rabilrbl/music-liked-sync/internal/model"
	"github.com/xrash/smetrics"
	"golang.org/x/text/runes"
	"golang.org/x/text/transform"
	"golang.org/x/text/unicode/norm"
)

var CommonTitleSuffixRE = regexp.MustCompile(`(?i)\s*(?:[-–—:]\s*)?\(?\b(?:remaster(?:ed)?(?:\s*\d{2,4})?|\d{4}\s*remaster(?:ed)?|deluxe(?:\s+edition)?|expanded(?:\s+edition)?|explicit|clean|single version|album version|radio edit|edit|live|mono|stereo|from .*|official audio|official video|official music video|official lyric video|lyric video|lyrics|audio only|video only|music video|full video|music audio|high quality|hq|hd|topic|original motion picture soundtrack|soundtrack|ost)\b\)?\s*$`)
var ArtistSplitRE = regexp.MustCompile(`(?i)\s*(?:,|/|&| x | and | feat\.? | ft\.? | featuring )\s*`)

func asciiLower(s string) string {
	t := transform.Chain(norm.NFD, runes.Remove(runes.In(unicode.Mn)), norm.NFC)
	result, _, _ := transform.String(t, s)
	return strings.ToLower(result)
}

func NormalizeText(value string, artists []string) string {
	text := asciiLower(value)
	text = strings.ReplaceAll(text, "&", " and ")

	// Handle common abbreviations
	text = regexp.MustCompile(`\bu\b`).ReplaceAllString(text, "you")
	text = regexp.MustCompile(`\br\b`).ReplaceAllString(text, "are")
	text = regexp.MustCompile(`\bw/\b`).ReplaceAllString(text, "with")
	text = regexp.MustCompile(`\bw/o\b`).ReplaceAllString(text, "without")

	// Aggressively strip metadata after common YTM delimiters
	text = regexp.MustCompile(`\bcoke studio\s*\|\s*season\s*\d+\s*\|\s*`).ReplaceAllString(text, "")

	if len(artists) > 0 {
		for _, artist := range artists {
			normA := asciiLower(artist)
			text = regexp.MustCompile(`(?i)^`+regexp.QuoteMeta(normA)+`\s*[-–—:]\s*`).ReplaceAllString(text, "")
			text = regexp.MustCompile(`(?i)^`+regexp.QuoteMeta(normA)+`\s*\|\s*`).ReplaceAllString(text, "")
		}
	}

	text = regexp.MustCompile(`\s*\|\s*.*$`).ReplaceAllString(text, "")
	text = regexp.MustCompile(`(?i)\s+\(?(?:feat|ft|featuring)\.?\s+.*$`).ReplaceAllString(text, "")

	previous := ""
	for previous != text {
		previous = text
		text = CommonTitleSuffixRE.ReplaceAllString(text, "")
	}

	text = regexp.MustCompile(`\([^)]*\)|\[[^]]*\]`).ReplaceAllString(text, " ")
	text = regexp.MustCompile(`[^a-z0-9\s]+`).ReplaceAllString(text, " ")
	text = regexp.MustCompile(`\s+`).ReplaceAllString(text, " ")
	return strings.TrimSpace(text)
}

func NormalizeArtist(value string) string {
	text := NormalizeText(value, nil)
	return strings.ReplaceAll(text, " ", "")
}

func NormalizeKey(title string, artists []string) string {
	normArtists := make([]string, 0, len(artists))
	for _, a := range artists {
		if a != "" {
			normArtists = append(normArtists, NormalizeArtist(a))
		}
	}
	sort.Strings(normArtists)
	artistPart := strings.Join(normArtists, "+")
	return fmt.Sprintf("%s::%s", NormalizeText(title, artists), artistPart)
}

func ArtistMatches(left, right []string) bool {
	leftNorm := make([]string, 0, len(left))
	for _, a := range left {
		if a != "" {
			leftNorm = append(leftNorm, NormalizeArtist(a))
		}
	}
	rightNorm := make([]string, 0, len(right))
	for _, a := range right {
		if a != "" {
			rightNorm = append(rightNorm, NormalizeArtist(a))
		}
	}
	if len(leftNorm) == 0 || len(rightNorm) == 0 {
		return false
	}
	for _, l := range leftNorm {
		for _, r := range rightNorm {
			if l == r || strings.Contains(l, r) || strings.Contains(r, l) {
				return true
			}
			if smetrics.JaroWinkler(l, r, 0.1, 4) >= 0.86 {
				return true
			}
		}
	}
	return false
}

func TrackSimilarity(wanted, candidate model.Track) float64 {
	titleScore := smetrics.JaroWinkler(
		NormalizeText(wanted.Title, wanted.Artists),
		NormalizeText(candidate.Title, candidate.Artists),
		0.1, 4,
	)
	artistScore := 0.0
	if ArtistMatches(wanted.Artists, candidate.Artists) {
		artistScore = 1.0
	}
	durationScore := 0.0
	if wanted.DurationMs != nil && candidate.DurationMs != nil {
		delta := math.Abs(float64(*wanted.DurationMs - *candidate.DurationMs))
		durationScore = math.Max(0.0, 1.0-delta/30000.0)
	}
	return (titleScore * 0.62) + (artistScore * 0.33) + (durationScore * 0.05)
}

func BestMatch(wanted model.Track, candidates []model.Track, threshold float64) *model.Track {
	if len(candidates) == 0 {
		return nil
	}
	wantedKey := NormalizeKey(wanted.Title, wanted.Artists)
	for _, candidate := range candidates {
		if NormalizeKey(candidate.Title, candidate.Artists) == wantedKey {
			return &candidate
		}
	}

	type scoredTrack struct {
		score float64
		track model.Track
	}
	scored := make([]scoredTrack, len(candidates))
	for i, c := range candidates {
		scored[i] = scoredTrack{score: TrackSimilarity(wanted, c), track: c}
	}
	sort.Slice(scored, func(i, j int) bool {
		return scored[i].score > scored[j].score
	})

	best := scored[0]
	if best.score >= threshold && ArtistMatches(wanted.Artists, best.track.Artists) {
		return &best.track
	}
	return nil
}

func PrimarySearchArtist(artists []string) string {
	for _, artist := range artists {
		parts := ArtistSplitRE.Split(artist, -1)
		for _, part := range parts {
			cleaned := strings.Trim(part, " -")
			if cleaned != "" {
				return cleaned
			}
		}
	}
	return ""
}

func TruncateQuery(query string, limit int) string {
	query = strings.TrimSpace(regexp.MustCompile(`\s+`).ReplaceAllString(query, " "))
	if len(query) <= limit {
		return query
	}
	truncated := query[:limit]
	lastSpace := strings.LastIndex(truncated, " ")
	if lastSpace != -1 {
		truncated = strings.TrimSpace(truncated[:lastSpace])
	}
	if truncated == "" {
		return query[:limit]
	}
	return truncated
}
