package music_liked_sync

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	_ "modernc.org/sqlite"
)

type SyncCache struct {
	path string
	db   *sql.DB
}

func NewSyncCache(path string) (*SyncCache, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return nil, err
	}

	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}

	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, err
	}

	queries := []string{
		`CREATE TABLE IF NOT EXISTS matches (
			direction TEXT NOT NULL,
			source_key TEXT NOT NULL,
			source_track_json TEXT NOT NULL,
			target_track_json TEXT NOT NULL,
			updated_at REAL NOT NULL,
			PRIMARY KEY (direction, source_key)
		)`,
		`CREATE TABLE IF NOT EXISTS liked_tracks (
			service TEXT NOT NULL,
			source_id TEXT NOT NULL,
			updated_at REAL NOT NULL,
			PRIMARY KEY (service, source_id)
		)`,
		`CREATE TABLE IF NOT EXISTS library_cache (
			service TEXT NOT NULL PRIMARY KEY,
			tracks_json TEXT NOT NULL,
			fetched_at REAL NOT NULL
		)`,
	}

	for _, q := range queries {
		if _, err := db.Exec(q); err != nil {
			return nil, err
		}
	}

	return &SyncCache{path: path, db: db}, nil
}

func (c *SyncCache) Close() error {
	return c.db.Close()
}

func (c *SyncCache) StoreMatch(direction string, source, target Track) error {
	sourceKey := NormalizeKey(source.Title, source.Artists)
	sourceJSON, _ := json.Marshal(source)
	targetJSON, _ := json.Marshal(target)
	now := float64(time.Now().UnixNano()) / 1e9

	_, err := c.db.Exec(`
		INSERT INTO matches(direction, source_key, source_track_json, target_track_json, updated_at)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(direction, source_key) DO UPDATE SET
			source_track_json=excluded.source_track_json,
			target_track_json=excluded.target_track_json,
			updated_at=excluded.updated_at
	`, direction, sourceKey, string(sourceJSON), string(targetJSON), now)
	return err
}

func (c *SyncCache) GetMatch(direction string, source Track) (*Track, error) {
	sourceKey := NormalizeKey(source.Title, source.Artists)
	var targetJSON string
	err := c.db.QueryRow("SELECT target_track_json FROM matches WHERE direction = ? AND source_key = ?", direction, sourceKey).Scan(&targetJSON)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	var track Track
	if err := json.Unmarshal([]byte(targetJSON), &track); err != nil {
		return nil, err
	}
	return &track, nil
}

func (c *SyncCache) MarkLiked(service, sourceID string) error {
	return c.MarkLikedMany(service, []string{sourceID})
}

func (c *SyncCache) MarkLikedMany(service string, sourceIDs []string) error {
	if len(sourceIDs) == 0 {
		return nil
	}
	now := float64(time.Now().UnixNano()) / 1e9

	tx, err := c.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()

	stmt, err := tx.Prepare(`
		INSERT INTO liked_tracks(service, source_id, updated_at)
		VALUES (?, ?, ?)
		ON CONFLICT(service, source_id) DO UPDATE SET
			updated_at=excluded.updated_at
	`)
	if err != nil {
		return err
	}
	defer stmt.Close()

	for _, id := range sourceIDs {
		if id == "" {
			continue
		}
		if _, err := stmt.Exec(service, id, now); err != nil {
			return err
		}
	}

	return tx.Commit()
}

func (c *SyncCache) IsLiked(service, sourceID string) (bool, error) {
	var exists int
	err := c.db.QueryRow("SELECT 1 FROM liked_tracks WHERE service = ? AND source_id = ? LIMIT 1", service, sourceID).Scan(&exists)
	if err == sql.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}

func (c *SyncCache) StoreLibrary(service string, tracks []Track) error {
	payload, err := json.Marshal(tracks)
	if err != nil {
		return err
	}
	now := float64(time.Now().UnixNano()) / 1e9

	_, err = c.db.Exec(`
		INSERT INTO library_cache(service, tracks_json, fetched_at)
		VALUES (?, ?, ?)
		ON CONFLICT(service) DO UPDATE SET
			tracks_json=excluded.tracks_json,
			fetched_at=excluded.fetched_at
	`, service, string(payload), now)
	return err
}

func (c *SyncCache) GetLibrary(service string, maxAgeSeconds float64) ([]Track, error) {
	if maxAgeSeconds <= 0 {
		return nil, nil
	}

	var tracksJSON string
	var fetchedAt float64
	err := c.db.QueryRow("SELECT tracks_json, fetched_at FROM library_cache WHERE service = ?", service).Scan(&tracksJSON, &fetchedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}

	if (float64(time.Now().UnixNano())/1e9 - fetchedAt) > maxAgeSeconds {
		return nil, nil
	}

	var tracks []Track
	if err := json.Unmarshal([]byte(tracksJSON), &tracks); err != nil {
		return nil, err
	}
	return tracks, nil
}
