package music_liked_sync

import (
	"testing"
)

func TestComputeMissing(t *testing.T) {
	t1 := Track{Title: "You And Me", Artists: []string{"Artist"}, SourceID: "s1"}
	t2 := Track{Title: "U and Me", Artists: []string{"Artist"}, SourceID: "s2"}
	t3 := Track{Title: "Different", Artists: []string{"Artist"}, SourceID: "s3"}

	// If t1 is in 'right', t2 should also be considered matched due to fuzzy matching
	missing := ComputeMissing([]Track{t1, t2, t3}, []Track{t1}, false)
	if len(missing) != 1 {
		t.Errorf("Expected 1 missing track, got %d", len(missing))
	} else if missing[0].SourceID != "s3" {
		t.Errorf("Expected s3 to be missing, got %s", missing[0].SourceID)
	}

	// Exact match check
	missing2 := ComputeMissing([]Track{t1}, []Track{t1}, false)
	if len(missing2) != 0 {
		t.Errorf("Expected 0 missing tracks, got %d", len(missing2))
	}
}

func TestResolveMatches(t *testing.T) {
	wanted := []Track{
		{Title: "Believer", Artists: []string{"Imagine Dragons"}, SourceID: "spotify:track:1"},
	}

	searchFn := func(Track) ([]Track, error) {
		return []Track{{Title: "Believer", Artists: []string{"Imagine Dragons"}, SourceID: "yt1"}}, nil
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
