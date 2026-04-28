package sync

import (
	"testing"

	"github.com/rabilrbl/music-liked-sync/internal/model"
)

func TestComputeMissing(t *testing.T) {
	t1 := model.Track{Title: "You And Me", Artists: []string{"Artist"}, SourceID: "s1"}
	t2 := model.Track{Title: "U and Me", Artists: []string{"Artist"}, SourceID: "s2"}
	t3 := model.Track{Title: "Different", Artists: []string{"Artist"}, SourceID: "s3"}

	// If t1 is in 'right', t2 should also be considered matched due to fuzzy matching
	missing := ComputeMissing([]model.Track{t1, t2, t3}, []model.Track{t1}, false)
	if len(missing) != 1 {
		t.Errorf("Expected 1 missing track, got %d", len(missing))
	} else if missing[0].SourceID != "s3" {
		t.Errorf("Expected s3 to be missing, got %s", missing[0].SourceID)
	}

	// Exact match check
	missing2 := ComputeMissing([]model.Track{t1}, []model.Track{t1}, false)
	if len(missing2) != 0 {
		t.Errorf("Expected 0 missing tracks, got %d", len(missing2))
	}
}

func TestResolveMatches(t *testing.T) {
	wanted := []model.Track{
		{Title: "Believer", Artists: []string{"Imagine Dragons"}, SourceID: "spotify:track:1"},
	}

	searchFn := func(model.Track) ([]model.Track, error) {
		return []model.Track{{Title: "Believer", Artists: []string{"Imagine Dragons"}, SourceID: "yt1"}}, nil
	}

	matched, unmatched, err := ResolveMatches(wanted, searchFn, nil, "test", 50, 0, nil, "", false, false, false)
	if err != nil {
		t.Fatalf("ResolveMatches failed: %v", err)
	}

	if len(matched) != 1 {
		t.Errorf("Expected 1 match, got %d", len(matched))
	}
	if len(unmatched) != 0 {
		t.Errorf("Expected 0 unmatched, got %d", len(unmatched))
	}
}
