package app

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/rabilrbl/music-liked-sync/internal/cache"
	"github.com/rabilrbl/music-liked-sync/internal/model"
	"github.com/rabilrbl/music-liked-sync/internal/spotify"
	"github.com/rabilrbl/music-liked-sync/internal/sync"
	"github.com/rabilrbl/music-liked-sync/internal/ytmusic"
	"github.com/spf13/cobra"
)

type Options struct {
	Market          string
	Apply           bool
	MaxAdd          int
	BatchSize       int
	BatchDelay      float64
	CacheDB         string
	CacheLibraryTTL float64
	NoCacheRead     bool
	NoCacheWrite    bool
	SpotifyToYTM    bool
	YTMToSpotify    bool
	ReportFile      string
}

func NewRootCommand() *cobra.Command {
	opts := Options{}

	cmd := &cobra.Command{
		Use:   "music-liked-sync",
		Short: "Sync Spotify and YouTube Music liked songs",
		RunE: func(cmd *cobra.Command, args []string) error {
			return Run(opts, cmd)
		},
	}

	cmd.Flags().StringVar(&opts.Market, "market", "IN", "Spotify market code")
	cmd.Flags().BoolVar(&opts.Apply, "apply", false, "actually save/like matched tracks")
	cmd.Flags().IntVar(&opts.MaxAdd, "max-add", 0, "optional cap on tracks to add per direction")
	cmd.Flags().IntVar(&opts.BatchSize, "batch-size", 50, "batch size")
	cmd.Flags().Float64Var(&opts.BatchDelay, "batch-delay", 1.0, "batch delay")
	cmd.Flags().StringVar(&opts.CacheDB, "cache-db", "state/sync-cache.sqlite3", "cache db path")
	cmd.Flags().Float64Var(&opts.CacheLibraryTTL, "cache-library-ttl", 0.0, "cache library TTL")
	cmd.Flags().BoolVar(&opts.NoCacheRead, "no-cache-read", false, "disable cache read")
	cmd.Flags().BoolVar(&opts.NoCacheWrite, "no-cache-write", false, "disable cache write")
	cmd.Flags().BoolVar(&opts.SpotifyToYTM, "spotify-to-ytm", false, "only sync Spotify to YTM")
	cmd.Flags().BoolVar(&opts.YTMToSpotify, "ytm-to-spotify", false, "only sync YTM to Spotify")
	cmd.Flags().StringVar(&opts.ReportFile, "report", "sync-report.json", "report file path")

	return cmd
}

func Run(opts Options, cmd *cobra.Command) error {
	vprint := func(format string, a ...interface{}) {}

	// Ensure absolute paths
	absPath := func(p string) string {
		if filepath.IsAbs(p) {
			return p
		}
		cwd, _ := os.Getwd()
		return filepath.Join(cwd, p)
	}

	ytSessionDir := absPath(ytmusic.DefaultYTBrowserSessionDir)
	spotifySessionDir := absPath(spotify.DefaultSpotifyWebSessionDir)
	cachePath := absPath(opts.CacheDB)

	ytAuth, err := ytmusic.EnsureYTBrowserAuth(ytSessionDir, absPath(ytmusic.DefaultYTBrowserLockFile), false, ytmusic.DefaultYTBrowserLoginTimeout)
	if err != nil {
		return err
	}

	spotifyBackend, err := spotify.NewSpotifyBackend(opts.Market, spotifySessionDir, absPath(spotify.DefaultSpotifyWebLockFile), false, spotify.DefaultSpotifyWebLoginTimeout)
	if err != nil {
		return err
	}

	c, err := cache.NewSyncCache(cachePath)
	if err != nil {
		return err
	}
	defer c.Close()

	ytm := ytmusic.NewYTMusicBackend(ytAuth)

	var spotifyLiked []model.Track
	if !opts.NoCacheRead {
		spotifyLiked, _ = c.GetLibrary("spotify", opts.CacheLibraryTTL)
	}
	if spotifyLiked == nil {
		spotifyLiked, err = spotifyBackend.LikedTracks(false)
		if err != nil {
			return err
		}
		if !opts.NoCacheWrite {
			c.StoreLibrary("spotify", spotifyLiked)
		}
	} else {
		vprint("Loaded %d Spotify tracks from cache", len(spotifyLiked))
	}

	var ytmLiked []model.Track
	if !opts.NoCacheRead {
		ytmLiked, _ = c.GetLibrary("ytm", opts.CacheLibraryTTL)
	}
	if ytmLiked == nil {
		ytmLiked, err = ytm.LikedTracks(false)
		if err != nil {
			return err
		}
		if !opts.NoCacheWrite {
			c.StoreLibrary("ytm", ytmLiked)
		}
	} else {
		vprint("Loaded %d YouTube Music tracks from cache", len(ytmLiked))
	}

	doSpotifyToYTM := opts.SpotifyToYTM || !opts.YTMToSpotify
	doYTMToSpotify := opts.YTMToSpotify || !opts.SpotifyToYTM

	report := make(map[string]interface{})
	report["apply"] = opts.Apply
	report["spotify_liked_count"] = len(spotifyLiked)
	report["ytm_liked_count"] = len(ytmLiked)

	var maxAddPtr *int
	if cmd.Flags().Changed("max-add") {
		maxAddPtr = &opts.MaxAdd
	}

	if doSpotifyToYTM {
		missing := sync.ComputeMissing(spotifyLiked, ytmLiked, false)
		vprint("Spotify → YTM: %d tracks missing in YTM", len(missing))
		matched, unmatched, err := sync.ResolveMatches(
			missing,
			func(t model.Track) ([]model.Track, error) { return ytm.SearchTrack(t, 5) },
			maxAddPtr,
			"Spotify → YTM",
			opts.BatchSize,
			opts.BatchDelay,
			c,
			"spotify_to_ytm",
			!opts.NoCacheRead,
			!opts.NoCacheWrite,
			false,
		)
		if err != nil {
			return err
		}

		if opts.Apply {
			var toLike []model.Track
			for _, m := range matched {
				liked := false
				if !opts.NoCacheRead {
					liked, _ = c.IsLiked("ytm", m.Target.SourceID)
				}
				if !liked {
					toLike = append(toLike, m.Target)
				}
			}
			vprint("Spotify → YTM: Liking %d tracks on YTM", len(toLike))
			if err := ytm.LikeTracks(toLike, opts.BatchSize, opts.BatchDelay, false); err != nil {
				return err
			}
			if !opts.NoCacheWrite {
				var ids []string
				for _, t := range toLike {
					ids = append(ids, t.SourceID)
				}
				c.MarkLikedMany("ytm", ids)
			}
		}
		report["spotify_to_ytm"] = map[string]interface{}{
			"missing_count": len(missing),
			"matched_count": len(matched),
			"matched":       matched,
			"unmatched":     unmatched,
		}
	}

	if doYTMToSpotify {
		missing := sync.ComputeMissing(ytmLiked, spotifyLiked, false)
		vprint("YTM → Spotify: %d tracks missing in Spotify", len(missing))
		matched, unmatched, err := sync.ResolveMatches(
			missing,
			func(t model.Track) ([]model.Track, error) { return spotifyBackend.SearchTrack(t, 5) },
			maxAddPtr,
			"YTM → Spotify",
			opts.BatchSize,
			opts.BatchDelay,
			c,
			"ytm_to_spotify",
			!opts.NoCacheRead,
			!opts.NoCacheWrite,
			false,
		)
		if err != nil {
			return err
		}

		if opts.Apply {
			var toSave []model.Track
			for _, m := range matched {
				liked := false
				if !opts.NoCacheRead {
					liked, _ = c.IsLiked("spotify", m.Target.SourceID)
				}
				if !liked {
					toSave = append(toSave, m.Target)
				}
			}
			vprint("YTM → Spotify: Saving %d tracks to Spotify", len(toSave))
			if err := spotifyBackend.SaveTracks(toSave, opts.BatchSize, opts.BatchDelay, false); err != nil {
				return err
			}
			if !opts.NoCacheWrite {
				var ids []string
				for _, t := range toSave {
					ids = append(ids, t.SourceID)
				}
				c.MarkLikedMany("spotify", ids)
			}
		}
		report["ytm_to_spotify"] = map[string]interface{}{
			"missing_count": len(missing),
			"matched_count": len(matched),
			"matched":       matched,
			"unmatched":     unmatched,
		}
	}

	reportData, _ := json.MarshalIndent(report, "", "  ")
	os.WriteFile(opts.ReportFile, reportData, 0644)

	summary := map[string]interface{}{
		"apply":               opts.Apply,
		"spotify_liked_count": len(spotifyLiked),
		"ytm_liked_count":     len(ytmLiked),
		"report":              opts.ReportFile,
	}
	summaryData, _ := json.MarshalIndent(summary, "", "  ")
	fmt.Println(string(summaryData))

	return nil
}
