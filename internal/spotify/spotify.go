package spotify

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/go-resty/resty/v2"
	"github.com/gofrs/flock"
	"github.com/playwright-community/playwright-go"
	"github.com/rabilrbl/music-liked-sync/internal/model"
	mls_sync "github.com/rabilrbl/music-liked-sync/internal/sync"
)

const (
	SpotifyWebOrigin              = "https://open.spotify.com"
	SpotifyWebTokenURLPrefix      = "https://open.spotify.com/api/token"
	SpotifyWebPathfinderURL       = "https://api-partner.spotify.com/pathfinder/v2/query"
	SpotifyWebRequiredCookie      = "sp_dc"
	DefaultSpotifyWebSessionDir   = "auth/spotify-web-session"
	DefaultSpotifyWebLockFile     = "state/locks/spotify-web-session.lock"
	DefaultSpotifyWebLoginTimeout = 300.0
)

type SpotifyWebSessionState struct {
	AccessToken string  `json:"access_token"`
	UserAgent   string  `json:"user_agent"`
	ClientToken *string `json:"client_token"`
	AppVersion  *string `json:"app_version"`
}

type SpotifyWebClient struct {
	tokenProvider func() (*SpotifyWebSessionState, error)
	state         *SpotifyWebSessionState
	httpClient    *resty.Client
}

func NewSpotifyWebClient(tokenProvider func() (*SpotifyWebSessionState, error)) (*SpotifyWebClient, error) {
	state, err := tokenProvider()
	if err != nil {
		return nil, err
	}
	return &SpotifyWebClient{
		tokenProvider: tokenProvider,
		state:         state,
		httpClient:    resty.New(),
	}, nil
}

func (c *SpotifyWebClient) refreshState() error {
	state, err := c.tokenProvider()
	if err != nil {
		return err
	}
	c.state = state
	return nil
}

func (c *SpotifyWebClient) pathfinder(payload interface{}) (map[string]interface{}, error) {
	resp, err := c.doPathfinder(payload)
	if err != nil {
		if strings.Contains(err.Error(), "401") {
			if err := c.refreshState(); err != nil {
				return nil, err
			}
			return c.doPathfinder(payload)
		}
		return nil, err
	}
	return resp, nil
}

func (c *SpotifyWebClient) doPathfinder(payload interface{}) (map[string]interface{}, error) {
	req := c.httpClient.R().
		SetHeader("accept", "application/json").
		SetHeader("app-platform", "WebPlayer").
		SetHeader("authorization", "Bearer "+c.state.AccessToken).
		SetHeader("content-type", "application/json;charset=UTF-8").
		SetHeader("origin", SpotifyWebOrigin).
		SetHeader("referer", SpotifyWebOrigin+"/").
		SetHeader("user-agent", c.state.UserAgent).
		SetBody(payload)

	if c.state.ClientToken != nil {
		req.SetHeader("client-token", *c.state.ClientToken)
	}
	if c.state.AppVersion != nil {
		req.SetHeader("spotify-app-version", *c.state.AppVersion)
	}

	resp, err := req.Post(SpotifyWebPathfinderURL)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode() != http.StatusOK {
		return nil, fmt.Errorf("Spotify Pathfinder HTTP %d: %s", resp.StatusCode(), resp.String())
	}

	var result map[string]interface{}
	if err := json.Unmarshal(resp.Body(), &result); err != nil {
		return nil, err
	}

	if errors, ok := result["errors"]; ok {
		return nil, fmt.Errorf("Spotify Pathfinder returned errors: %v", errors)
	}

	return result, nil
}

func (c *SpotifyWebClient) FetchLibraryTracks(limit, offset int) (map[string]interface{}, error) {
	payload := map[string]interface{}{
		"variables":     map[string]interface{}{"offset": offset, "limit": limit},
		"operationName": "fetchLibraryTracks",
		"extensions": map[string]interface{}{
			"persistedQuery": map[string]interface{}{
				"version":    1,
				"sha256Hash": "087278b20b743578a6262c2b0b4bcd20d879c503cc359a2285baf083ef944240",
			},
		},
	}
	return c.pathfinder(payload)
}

func (c *SpotifyWebClient) Search(q string, limit int) (map[string]interface{}, error) {
	payload := map[string]interface{}{
		"variables": map[string]interface{}{
			"query":                          q,
			"limit":                          int64(mathMax(limit, 10)),
			"offset":                         0,
			"numberOfTopResults":             int64(mathMax(limit, 10)),
			"includeArtistHasConcertsField":  false,
			"includeAudiobooks":              true,
			"includeAuthors":                 false,
			"includePreReleases":             true,
			"includeEpisodeContentRatingsV2": false,
			"sectionFilters":                 []string{"GENERIC", "VIDEO_CONTENT"},
		},
		"operationName": "searchTopResultsList",
		"extensions": map[string]interface{}{
			"persistedQuery": map[string]interface{}{
				"version":    1,
				"sha256Hash": "75a88491b7c54a02065a24d6e836121ab20ca42d1bede25a0e06fe5018033ffe",
			},
		},
	}
	return c.pathfinder(payload)
}

func (c *SpotifyWebClient) AddToLibrary(uris []string) (map[string]interface{}, error) {
	payload := map[string]interface{}{
		"variables":     map[string]interface{}{"libraryItemUris": uris},
		"operationName": "addToLibrary",
		"extensions": map[string]interface{}{
			"persistedQuery": map[string]interface{}{
				"version":    1,
				"sha256Hash": "7c5a69420e2bfae3da5cc4e14cbc8bb3f6090f80afc00ffc179177f19be3f33d",
			},
		},
	}
	return c.pathfinder(payload)
}

func mathMax(a, b int) int {
	if a > b {
		return a
	}
	return b
}

type SpotifyBackend struct {
	market     string
	client     *SpotifyWebClient
	sessionDir string
	lockFile   string
}

func NewSpotifyBackend(market, sessionDir, lockFile string, headless bool, timeout float64) (*SpotifyBackend, error) {
	client, err := NewSpotifyWebClient(func() (*SpotifyWebSessionState, error) {
		return EnsureSpotifyWebSessionState(sessionDir, lockFile, headless, timeout)
	})
	if err != nil {
		return nil, err
	}
	return &SpotifyBackend{
		market:     market,
		client:     client,
		sessionDir: sessionDir,
		lockFile:   lockFile,
	}, nil
}

func EnsureSpotifyWebSessionState(sessionDir, lockFile string, headless bool, timeout float64) (*SpotifyWebSessionState, error) {
	if err := os.MkdirAll(sessionDir, 0755); err != nil {
		return nil, err
	}

	fileLock := flock.New(lockFile)
	locked, err := fileLock.TryLock()
	if err != nil {
		return nil, err
	}
	if !locked {
		return nil, fmt.Errorf("browser session is already active (lock held): %s", lockFile)
	}
	defer fileLock.Unlock()

	pw, err := playwright.Run()
	if err != nil {
		return nil, err
	}
	defer pw.Stop()

	context, err := pw.Chromium.LaunchPersistentContext(sessionDir, playwright.BrowserTypeLaunchPersistentContextOptions{
		Headless: playwright.Bool(headless),
		Args:     []string{"--disable-blink-features=AutomationControlled"},
	})
	if err != nil {
		return nil, err
	}
	defer context.Close()

	page := context.Pages()[0]
	if page == nil {
		page, _ = context.NewPage()
	}

	var state *SpotifyWebSessionState
	var stateMu sync.Mutex

	page.OnRequest(func(request playwright.Request) {
		if request.URL() == SpotifyWebPathfinderURL {
			headers := request.Headers()
			stateMu.Lock()
			defer stateMu.Unlock()
			if state == nil {
				state = &SpotifyWebSessionState{}
			}
			if val, ok := headers["client-token"]; ok {
				state.ClientToken = &val
			}
			if val, ok := headers["spotify-app-version"]; ok {
				state.AppVersion = &val
			}
		}
	})

	tokenChan := make(chan map[string]interface{}, 1)
	page.OnResponse(func(response playwright.Response) {
		if strings.HasPrefix(response.URL(), SpotifyWebTokenURLPrefix) && response.Status() == 200 {
			var data map[string]interface{}
			body, _ := response.Body()
			json.Unmarshal(body, &data)
			select {
			case tokenChan <- data:
			default:
			}
		}
	})

	hasCookie := func() bool {
		cookies, _ := context.Cookies(SpotifyWebOrigin)
		for _, c := range cookies {
			if c.Name == SpotifyWebRequiredCookie {
				return true
			}
		}
		return false
	}

	if !hasCookie() {
		fmt.Fprintf(os.Stderr, "Spotify Web Player login required. Complete login in the opened browser window; this browser profile will be reused on future runs.\n")
		page.Goto(SpotifyWebOrigin)
	}

	deadline := time.Now().Add(time.Duration(timeout) * time.Second)
	for !hasCookie() && time.Now().Before(deadline) {
		time.Sleep(2 * time.Second)
	}

	if !hasCookie() {
		return nil, fmt.Errorf("Spotify Web Player session is not logged in; missing %s", SpotifyWebRequiredCookie)
	}

	if _, err := page.Goto(SpotifyWebOrigin); err != nil {
		return nil, err
	}

	var tokenData map[string]interface{}
	select {
	case tokenData = <-tokenChan:
	case <-time.After(30 * time.Second):
		return nil, fmt.Errorf("Spotify Web Player token request did not complete")
	}

	accessToken, ok := tokenData["accessToken"].(string)
	if !ok {
		accessToken, _ = tokenData["access_token"].(string)
	}
	if accessToken == "" {
		return nil, fmt.Errorf("Spotify Web Player token response did not include accessToken")
	}

	if isAnon, _ := tokenData["isAnonymous"].(bool); isAnon {
		return nil, fmt.Errorf("Spotify Web Player returned an anonymous token; complete Spotify login, then rerun")
	}

	userAgentVal, _ := page.Evaluate("navigator.userAgent")

	stateMu.Lock()
	if state == nil {
		state = &SpotifyWebSessionState{}
	}
	state.AccessToken = accessToken
	state.UserAgent = userAgentVal.(string)
	stateMu.Unlock()

	return state, nil
}

func (b *SpotifyBackend) LikedTracks(verbose bool) ([]model.Track, error) {
	if verbose {
		fmt.Println("Fetching Spotify liked tracks library...")
	}

	firstPage, err := b.client.FetchLibraryTracks(50, 0)
	if err != nil {
		return nil, err
	}

	data := firstPage["data"].(map[string]interface{})
	me := data["me"].(map[string]interface{})
	library := me["library"].(map[string]interface{})
	tracksData := library["tracks"].(map[string]interface{})
	total := int(tracksData["totalCount"].(float64))

	if verbose {
		fmt.Printf("  Found %d tracks in Spotify library\n", total)
	}

	items := tracksData["items"].([]interface{})
	tracks := make([]model.Track, 0, total)
	for _, item := range items {
		if t := parseSpotifyTrack(item.(map[string]interface{})); t != nil {
			tracks = append(tracks, *t)
		}
	}

	for offset := 50; offset < total; offset += 50 {
		page, err := b.client.FetchLibraryTracks(50, offset)
		if err != nil {
			return nil, err
		}
		data := page["data"].(map[string]interface{})
		me := data["me"].(map[string]interface{})
		library := me["library"].(map[string]interface{})
		tracksData := library["tracks"].(map[string]interface{})
		items := tracksData["items"].([]interface{})
		for _, item := range items {
			if t := parseSpotifyTrack(item.(map[string]interface{})); t != nil {
				tracks = append(tracks, *t)
			}
		}
	}

	if verbose {
		fmt.Printf("  Finished fetching %d tracks from Spotify\n", len(tracks))
	}
	return tracks, nil
}

func parseSpotifyTrack(item map[string]interface{}) *model.Track {
	trackData, ok := item["track"].(map[string]interface{})
	if !ok {
		trackData = item
	}

	id, _ := trackData["id"].(string)
	name, _ := trackData["name"].(string)
	if id == "" || name == "" {
		// Fallback for GraphQL structure
		data, _ := item["data"].(map[string]interface{})
		if data != nil {
			id, _ = data["id"].(string)
			name, _ = data["name"].(string)
			trackData = data
		}
	}

	if id == "" || name == "" {
		return nil
	}

	var artists []string
	artistsList, _ := trackData["artists"].(map[string]interface{})
	if artistsList != nil {
		items, _ := artistsList["items"].([]interface{})
		for _, a := range items {
			aMap := a.(map[string]interface{})
			profile, _ := aMap["profile"].(map[string]interface{})
			if profile != nil {
				if n, ok := profile["name"].(string); ok {
					artists = append(artists, n)
				}
			} else if n, ok := aMap["name"].(string); ok {
				artists = append(artists, n)
			}
		}
	} else {
		// Legacy API structure
		items, _ := trackData["artists"].([]interface{})
		for _, a := range items {
			aMap := a.(map[string]interface{})
			if n, ok := aMap["name"].(string); ok {
				artists = append(artists, n)
			}
		}
	}

	var durationMs *int
	if d, ok := trackData["duration_ms"].(float64); ok {
		v := int(d)
		durationMs = &v
	} else if d, ok := trackData["duration"].(map[string]interface{}); ok {
		if ms, ok := d["totalMilliseconds"].(float64); ok {
			v := int(ms)
			durationMs = &v
		}
	}

	var albumName *string
	albumOfTrack, _ := trackData["albumOfTrack"].(map[string]interface{})
	if albumOfTrack != nil {
		if n, ok := albumOfTrack["name"].(string); ok {
			albumName = &n
		}
	} else {
		album, _ := trackData["album"].(map[string]interface{})
		if album != nil {
			if n, ok := album["name"].(string); ok {
				albumName = &n
			}
		}
	}

	return &model.Track{
		Title:      name,
		Artists:    artists,
		SourceID:   "spotify:track:" + id,
		DurationMs: durationMs,
		Album:      albumName,
	}
}

func (b *SpotifyBackend) SearchTrack(wanted model.Track, limit int) ([]model.Track, error) {
	queries := BuildSpotifySearchQueries(wanted)
	for _, query := range queries {
		page, err := b.client.Search(query, limit)
		if err != nil {
			continue
		}
		// GraphQL search results extraction is a bit deep
		var tracks []model.Track
		collectTracks(page, &tracks)
		if len(tracks) > 0 {
			return tracks, nil
		}
	}
	return nil, nil
}

func collectTracks(v interface{}, tracks *[]model.Track) {
	switch val := v.(type) {
	case map[string]interface{}:
		if val["__typename"] == "TrackResponseWrapper" || val["__typename"] == "Track" {
			if t := parseSpotifyTrack(val); t != nil {
				*tracks = append(*tracks, *t)
			}
			return
		}
		for _, child := range val {
			collectTracks(child, tracks)
		}
	case []interface{}:
		for _, child := range val {
			collectTracks(child, tracks)
		}
	}
}

func BuildSpotifySearchQueries(wanted model.Track) []string {
	seen := make(map[string]bool)
	var queries []string

	title := strings.TrimSpace(wanted.Title)
	normTitle := mls_sync.NormalizeText(title, wanted.Artists)
	primaryArtist := mls_sync.PrimarySearchArtist(wanted.Artists)
	var allArtists []string
	for _, a := range wanted.Artists {
		allArtists = append(allArtists, mls_sync.NormalizeArtist(a))
	}
	allArtistsStr := strings.Join(allArtists, " ")

	candidates := []string{
		fmt.Sprintf("track:%s artist:%s", title, primaryArtist),
		fmt.Sprintf("track:%s artist:%s", normTitle, primaryArtist),
		fmt.Sprintf("track:%s", title),
		fmt.Sprintf("track:%s", normTitle),
		fmt.Sprintf("%s %s", title, primaryArtist),
		fmt.Sprintf("%s %s", normTitle, primaryArtist),
		fmt.Sprintf("%s %s", normTitle, allArtistsStr),
		title,
		normTitle,
	}

	for _, c := range candidates {
		query := mls_sync.TruncateQuery(c, 240)
		if query != "" && !seen[query] {
			seen[query] = true
			queries = append(queries, query)
		}
	}
	return queries
}

func (b *SpotifyBackend) SaveTracks(tracks []model.Track, batchSize int, batchDelay float64, verbose bool) error {
	if verbose {
		fmt.Printf("Saving %d tracks to Spotify...\n", len(tracks))
	}

	ids := make([]string, len(tracks))
	for i, t := range tracks {
		parts := strings.Split(t.SourceID, ":")
		ids[i] = parts[len(parts)-1]
	}

	effectiveBatchSize := batchSize
	if effectiveBatchSize > 50 {
		effectiveBatchSize = 50
	}

	for i := 0; i < len(ids); i += effectiveBatchSize {
		end := i + effectiveBatchSize
		if end > len(ids) {
			end = len(ids)
		}
		chunk := ids[i:end]
		if verbose {
			fmt.Printf("  [SAVE] Batch %d/%d (%d tracks)\n", i/effectiveBatchSize+1, (len(ids)-1)/effectiveBatchSize+1, len(chunk))
		}
		if _, err := b.client.AddToLibrary(chunk); err != nil {
			return err
		}
		if batchDelay > 0 && end < len(ids) {
			time.Sleep(time.Duration(batchDelay * float64(time.Second)))
		}
	}
	return nil
}
