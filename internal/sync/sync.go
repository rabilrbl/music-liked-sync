package sync

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/rabilrbl/music-liked-sync/internal/model"
)

type MatchStore interface {
	GetMatch(direction string, source model.Track) (*model.Track, error)
	StoreMatch(direction string, source, target model.Track) error
}

func ComputeMissing(left, right []model.Track, verbose bool) []model.Track {
	rightKeys := make(map[string]bool)
	for _, t := range right {
		rightKeys[NormalizeKey(t.Title, t.Artists)] = true
	}

	rightByArtist := make(map[string][]model.Track)
	for _, t := range right {
		for _, a := range t.Artists {
			normA := NormalizeArtist(a)
			if normA != "" {
				rightByArtist[normA] = append(rightByArtist[normA], t)
			}
		}
	}

	var missing []model.Track
	for i, t := range left {
		if verbose && i%100 == 0 && i > 0 {
			fmt.Printf("  Comparing track %d/%d...\r", i, len(left))
		}

		key := NormalizeKey(t.Title, t.Artists)
		if rightKeys[key] {
			continue
		}

		var possibleCandidates []model.Track
		seenIDs := make(map[string]bool)
		for _, a := range t.Artists {
			normA := NormalizeArtist(a)
			for _, cand := range rightByArtist[normA] {
				if !seenIDs[cand.SourceID] {
					possibleCandidates = append(possibleCandidates, cand)
					seenIDs[cand.SourceID] = true
				}
			}
		}

		if len(possibleCandidates) > 0 && BestMatch(t, possibleCandidates, 0.82) != nil {
			continue
		}

		missing = append(missing, t)
	}

	if verbose {
		fmt.Print(strings.Repeat(" ", 90) + "\r")
	}
	return missing
}

func ResolveMatches(
	missing []model.Track,
	searchFn func(model.Track) ([]model.Track, error),
	maxAdd *int,
	label string,
	batchSize int,
	batchDelay float64,
	cache MatchStore,
	cacheDirection string,
	cacheRead bool,
	cacheWrite bool,
	verbose bool,
) ([]model.MatchedTrack, []model.Track, error) {
	var matched []model.MatchedTrack
	var unmatched []model.Track

	candidates := missing
	if maxAdd != nil && *maxAdd < len(missing) {
		candidates = missing[:*maxAdd]
	}

	if verbose {
		fmt.Printf("Resolving matches for %d tracks (%s)...\n", len(candidates), label)
	}

	for i, wanted := range candidates {
		if cache != nil && cacheDirection != "" && cacheRead {
			cached, err := cache.GetMatch(cacheDirection, wanted)
			if err == nil && cached != nil {
				if verbose {
					fmt.Printf("  [CACHE] %s -> %s\n", wanted.Display(), cached.Display())
				}
				matched = append(matched, model.MatchedTrack{Source: wanted, Target: *cached, Score: 1.0})
				continue
			}
		}

		searchRes, err := searchFn(wanted)
		if err != nil {
			fmt.Fprintf(os.Stderr, "\n%s: search failed for %s; treating as unresolved (%v)\n", label, wanted.Display(), err)
			unmatched = append(unmatched, wanted)
			continue
		}

		match := BestMatch(wanted, searchRes, 0.82)
		if match != nil {
			score := TrackSimilarity(wanted, *match)
			if verbose {
				fmt.Printf("  [MATCH] %s -> %s (score: %.2f)\n", wanted.Display(), match.Display(), score)
			}
			matched = append(matched, model.MatchedTrack{Source: wanted, Target: *match, Score: score})
			if cache != nil && cacheDirection != "" && cacheWrite {
				cache.StoreMatch(cacheDirection, wanted, *match)
			}
		} else {
			if verbose {
				fmt.Printf("  [MISS]  %s (no match in %d search results)\n", wanted.Display(), len(searchRes))
			}
			unmatched = append(unmatched, wanted)
		}

		if batchDelay > 0 && i < len(candidates)-1 && (i+1)%batchSize == 0 {
			time.Sleep(time.Duration(batchDelay * float64(time.Second)))
		}

		fmt.Printf("%s: %d matched, %d unmatched\r", label, len(matched), len(unmatched))
	}

	fmt.Print(strings.Repeat(" ", 90) + "\r")
	return matched, unmatched, nil
}
