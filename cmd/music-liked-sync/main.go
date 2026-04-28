package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	mls "github.com/rabil/music-liked-sync/internal/music_liked_sync"
	"github.com/spf13/cobra"
)

func main() {
	var (
		market          string
		apply           bool
		maxAdd          int
		batchSize       int
		batchDelay      float64
		cacheDB         string
		cacheLibraryTTL float64
		noCacheRead     bool
		noCacheWrite    bool
		spotifyToYTM    bool
		ytmToSpotify    bool
		reportFile      string
		workers         int
		verbose         bool
	)

	rootCmd := &cobra.Command{
		Use:   "music-liked-sync",
		Short: "Sync Spotify and YouTube Music liked songs",
		RunE: func(cmd *cobra.Command, args []string) error {
			vprint := func(format string, a ...interface{}) {
				if verbose {
					fmt.Printf(format+"\n", a...)
				}
			}

			// Ensure absolute paths
			absPath := func(p string) string {
				if filepath.IsAbs(p) {
					return p
				}
				cwd, _ := os.Getwd()
				return filepath.Join(cwd, p)
			}

			ytSessionDir := absPath(mls.DefaultYTBrowserSessionDir)
			spotifySessionDir := absPath(mls.DefaultSpotifyWebSessionDir)
			cachePath := absPath(cacheDB)

			ytAuth, err := mls.EnsureYTBrowserAuth(ytSessionDir, absPath(mls.DefaultYTBrowserLockFile), false, mls.DefaultYTBrowserLoginTimeout)
			if err != nil {
				return err
			}

			spotify, err := mls.NewSpotifyBackend(market, spotifySessionDir, absPath(mls.DefaultSpotifyWebLockFile), false, mls.DefaultSpotifyWebLoginTimeout)
			if err != nil {
				return err
			}

			cache, err := mls.NewSyncCache(cachePath)
			if err != nil {
				return err
			}
			defer cache.Close()

			ytm := mls.NewYTMusicBackend(ytAuth)

			var spotifyLiked []mls.Track
			if !noCacheRead {
				spotifyLiked, _ = cache.GetLibrary("spotify", cacheLibraryTTL)
			}
			if spotifyLiked == nil {
				spotifyLiked, err = spotify.LikedTracks(verbose)
				if err != nil {
					return err
				}
				if !noCacheWrite {
					cache.StoreLibrary("spotify", spotifyLiked)
				}
			} else {
				vprint("Loaded %d Spotify tracks from cache", len(spotifyLiked))
			}

			var ytmLiked []mls.Track
			if !noCacheRead {
				ytmLiked, _ = cache.GetLibrary("ytm", cacheLibraryTTL)
			}
			if ytmLiked == nil {
				ytmLiked, err = ytm.LikedTracks(verbose)
				if err != nil {
					return err
				}
				if !noCacheWrite {
					cache.StoreLibrary("ytm", ytmLiked)
				}
			} else {
				vprint("Loaded %d YouTube Music tracks from cache", len(ytmLiked))
			}

			doSpotifyToYTM := spotifyToYTM || !ytmToSpotify
			doYTMToSpotify := ytmToSpotify || !spotifyToYTM

			report := make(map[string]interface{})
			report["apply"] = apply
			report["spotify_liked_count"] = len(spotifyLiked)
			report["ytm_liked_count"] = len(ytmLiked)

			var maxAddPtr *int
			if cmd.Flags().Changed("max-add") {
				maxAddPtr = &maxAdd
			}

			if doSpotifyToYTM {
				missing := mls.ComputeMissing(spotifyLiked, ytmLiked, verbose)
				vprint("Spotify → YTM: %d tracks missing in YTM", len(missing))
				matched, unmatched, err := mls.ResolveMatches(
					missing,
					func(t mls.Track) ([]mls.Track, error) { return ytm.SearchTrack(t, 5) },
					maxAddPtr,
					"Spotify → YTM",
					batchSize,
					batchDelay,
					cache,
					"spotify_to_ytm",
					!noCacheRead,
					!noCacheWrite,
					verbose,
				)
				if err != nil {
					return err
				}

				if apply {
					var toLike []mls.Track
					for _, m := range matched {
						liked := false
						if !noCacheRead {
							liked, _ = cache.IsLiked("ytm", m.Target.SourceID)
						}
						if !liked {
							toLike = append(toLike, m.Target)
						}
					}
					vprint("Spotify → YTM: Liking %d tracks on YTM", len(toLike))
					if err := ytm.LikeTracks(toLike, batchSize, batchDelay, verbose); err != nil {
						return err
					}
					if !noCacheWrite {
						var ids []string
						for _, t := range toLike {
							ids = append(ids, t.SourceID)
						}
						cache.MarkLikedMany("ytm", ids)
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
				missing := mls.ComputeMissing(ytmLiked, spotifyLiked, verbose)
				vprint("YTM → Spotify: %d tracks missing in Spotify", len(missing))
				matched, unmatched, err := mls.ResolveMatches(
					missing,
					func(t mls.Track) ([]mls.Track, error) { return spotify.SearchTrack(t, 5) },
					maxAddPtr,
					"YTM → Spotify",
					batchSize,
					batchDelay,
					cache,
					"ytm_to_spotify",
					!noCacheRead,
					!noCacheWrite,
					verbose,
				)
				if err != nil {
					return err
				}

				if apply {
					var toSave []mls.Track
					for _, m := range matched {
						liked := false
						if !noCacheRead {
							liked, _ = cache.IsLiked("spotify", m.Target.SourceID)
						}
						if !liked {
							toSave = append(toSave, m.Target)
						}
					}
					vprint("YTM → Spotify: Saving %d tracks to Spotify", len(toSave))
					if err := spotify.SaveTracks(toSave, batchSize, batchDelay, verbose); err != nil {
						return err
					}
					if !noCacheWrite {
						var ids []string
						for _, t := range toSave {
							ids = append(ids, t.SourceID)
						}
						cache.MarkLikedMany("spotify", ids)
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
			os.WriteFile(reportFile, reportData, 0644)

			summary := map[string]interface{}{
				"apply":               apply,
				"spotify_liked_count": len(spotifyLiked),
				"ytm_liked_count":     len(ytmLiked),
				"report":              reportFile,
			}
			summaryData, _ := json.MarshalIndent(summary, "", "  ")
			fmt.Println(string(summaryData))

			return nil
		},
	}

	rootCmd.Flags().StringVar(&market, "market", mls.DefaultMarket, "Spotify market code")
	rootCmd.Flags().BoolVar(&apply, "apply", false, "actually save/like matched tracks")
	rootCmd.Flags().IntVar(&maxAdd, "max-add", 0, "optional cap on tracks to add per direction")
	rootCmd.Flags().IntVar(&batchSize, "batch-size", mls.DefaultBatchSize, "batch size")
	rootCmd.Flags().Float64Var(&batchDelay, "batch-delay", mls.DefaultBatchDelay, "batch delay")
	rootCmd.Flags().StringVar(&cacheDB, "cache-db", mls.DefaultCacheDB, "cache db path")
	rootCmd.Flags().Float64Var(&cacheLibraryTTL, "cache-library-ttl", mls.DefaultLibraryCacheTTL, "cache library TTL")
	rootCmd.Flags().BoolVar(&noCacheRead, "no-cache-read", false, "disable cache read")
	rootCmd.Flags().BoolVar(&noCacheWrite, "no-cache-write", false, "disable cache write")
	rootCmd.Flags().BoolVar(&spotifyToYTM, "spotify-to-ytm", false, "only sync Spotify to YTM")
	rootCmd.Flags().BoolVar(&ytmToSpotify, "ytm-to-spotify", false, "only sync YTM to Spotify")
	rootCmd.Flags().StringVar(&reportFile, "report", "sync-report.json", "report file path")
	rootCmd.Flags().IntVar(&workers, "workers", 4, "number of workers")
	rootCmd.Flags().BoolVar(&verbose, "verbose", false, "verbose output")

	if err := rootCmd.Execute(); err != nil {
		os.Exit(1)
	}
}
