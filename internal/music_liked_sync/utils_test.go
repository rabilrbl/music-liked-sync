package music_liked_sync

import (
	"testing"
)

func TestNormalizeText(t *testing.T) {
	tests := []struct {
		input    string
		artists  []string
		expected string
	}{
		{"Pasoori", []string{"Ali Sethi", "Shae Gill"}, "pasoori"},
		{"Pasoori (Official Video)", nil, "pasoori"},
		{"Artist - Song", []string{"Artist"}, "song"},
		{"Song | Metadata", nil, "song"},
		{"Song feat. Other", nil, "song"},
		{"Coke Studio | Season 14 | Pasoori", nil, "pasoori"},
		{"Song (Remastered 2022)", nil, "song"},
		{"Need Your Love - Remastered 2011", []string{"OneRepublic"}, "need your love"},
	}

	for _, tt := range tests {
		got := NormalizeText(tt.input, tt.artists)
		if got != tt.expected {
			t.Errorf("NormalizeText(%q, %v) = %q; want %q", tt.input, tt.artists, got, tt.expected)
		}
	}
}

func TestArtistMatches(t *testing.T) {
	tests := []struct {
		left     []string
		right    []string
		expected bool
	}{
		{[]string{"Ali Sethi"}, []string{"ali sethi"}, true},
		{[]string{"Ali Sethi"}, []string{"Shae Gill"}, false},
		{[]string{"Ali Sethi", "Shae Gill"}, []string{"Shae Gill"}, true},
		{[]string{"Alex Warren"}, []string{"alexwarren"}, true},
		{[]string{"Alex Warren"}, []string{"alxe warren"}, true}, // JaroWinkler should handle this
	}

	for _, tt := range tests {
		got := ArtistMatches(tt.left, tt.right)
		if got != tt.expected {
			t.Errorf("ArtistMatches(%v, %v) = %v; want %v", tt.left, tt.right, got, tt.expected)
		}
	}
}

func TestTrackSimilarity(t *testing.T) {
	dur180 := 180000
	dur181 := 181000
	dur220 := 220000
	t1 := Track{Title: "Song", Artists: []string{"Artist"}, SourceID: "1", DurationMs: &dur180}
	t2 := Track{Title: "Song", Artists: []string{"Artist"}, SourceID: "2", DurationMs: &dur181}
	t3 := Track{Title: "Song", Artists: []string{"Artist"}, SourceID: "3", DurationMs: &dur220}

	if TrackSimilarity(t1, t2) <= TrackSimilarity(t1, t3) {
		t.Errorf("Expected t1 similarity to t2 to be higher than to t3")
	}
}

func TestPrimarySearchArtist(t *testing.T) {
	tests := []struct {
		input    []string
		expected string
	}{
		{[]string{"DJ Raahul Pai, Ravi Sharma"}, "DJ Raahul Pai"},
		{[]string{" - ", "Actual Artist"}, "Actual Artist"},
		{[]string{}, ""},
	}

	for _, tt := range tests {
		got := PrimarySearchArtist(tt.input)
		if got != tt.expected {
			t.Errorf("PrimarySearchArtist(%v) = %q; want %q", tt.input, got, tt.expected)
		}
	}
}

func TestTruncateQuery(t *testing.T) {
	longQuery := "a b c d e f g h i j"
	truncated := TruncateQuery(longQuery, 10)
	if len(truncated) > 10 {
		t.Errorf("Truncated query too long: %q", truncated)
	}
	if TruncateQuery("short", 10) != "short" {
		t.Errorf("TruncateQuery failed for short string")
	}
}
