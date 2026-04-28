package music_liked_sync

import (
	"fmt"
	"os"
	"strings"
	"time"
)

func ComputeMissing(left, right []Track, verbose bool) []Track {
	rightKeys := make(map[string]bool)
	for _, t := range right {
		rightKeys[NormalizeKey(t.Title, t.Artists)] = true
	}

	rightByArtist := make(map[string][]Track)
	for _, t := range right {
		for _, a := range t.Artists {
			normA := NormalizeArtist(a)
			if normA != "" {
				rightByArtist[normA] = append(rightByArtist[normA], t)
			}
		}
	}

	var missing []Track
	for i, t := range left {
		if verbose && i%100 == 0 && i > 0 {
			fmt.Printf("  Comparing track %d/%d...\r", i, len(left))
		}

		key := NormalizeKey(t.Title, t.Artists)
		if rightKeys[key] {
			continue
		}

		var possibleCandidates []Track
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

type MatchResult struct {
	Source Track
	Target Track
}

func ResolveMatches(
	missing []Track,
	searchFn func(Track) ([]Track, error),
	maxAdd *int,
	label string,
	batchSize int,
	batchDelay float64,
	cache *SyncCache,
	cacheDirection string,
	cacheRead bool,
	cacheWrite bool,
	verbose bool,
) ([]MatchResult, []Track, error) {
	var matched []MatchResult
	var unmatched []Track

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
				matched = append(matched, MatchResult{Source: wanted, Target: *cached})
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
			if verbose {
				fmt.Printf("  [MATCH] %s -> %s\n", wanted.Display(), match.Display())
			}
			matched = append(matched, MatchResult{Source: wanted, Target: *match})
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
