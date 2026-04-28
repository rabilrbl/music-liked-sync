package music_liked_sync

import (
	"os"
	"testing"
)

func TestCache(t *testing.T) {
	dbPath := "test_cache.sqlite3"
	defer os.Remove(dbPath)

	cache, err := NewSyncCache(dbPath)
	if err != nil {
		t.Fatalf("Failed to create cache: %v", err)
	}
	defer cache.Close()

	track := Track{Title: "Song", Artists: []string{"Artist"}, SourceID: "123"}
	target := Track{Title: "Song Target", Artists: []string{"Artist"}, SourceID: "456"}

	if err := cache.StoreMatch("spotify_to_ytm", track, target); err != nil {
		t.Errorf("Failed to store match: %v", err)
	}

	got, err := cache.GetMatch("spotify_to_ytm", track)
	if err != nil {
		t.Errorf("Failed to get match: %v", err)
	}
	if got == nil || got.SourceID != "456" {
		t.Errorf("Got wrong match: %v", got)
	}

	if err := cache.MarkLiked("ytm", "456"); err != nil {
		t.Errorf("Failed to mark liked: %v", err)
	}

	liked, err := cache.IsLiked("ytm", "456")
	if err != nil || !liked {
		t.Errorf("IsLiked failed: %v, %v", err, liked)
	}
}
