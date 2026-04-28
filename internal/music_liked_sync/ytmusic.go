package music_liked_sync

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/go-resty/resty/v2"
	"github.com/gofrs/flock"
	"github.com/playwright-community/playwright-go"
)

const YTMusicBaseAPI = "https://music.youtube.com/youtubei/v1/"

var innertubeAPIKeyRE = regexp.MustCompile(`"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"`)

type YTMusicBackend struct {
	httpClient *resty.Client
	headers    map[string]string
}

func NewYTMusicBackend(headers map[string]string) *YTMusicBackend {
	client := resty.New()
	return &YTMusicBackend{
		httpClient: client,
		headers:    headers,
	}
}

func (b *YTMusicBackend) innertubeAPIKey() (string, error) {
	if apiKey := strings.TrimSpace(os.Getenv("YTMUSIC_INNERTUBE_API_KEY")); apiKey != "" {
		return apiKey, nil
	}

	resp, err := b.httpClient.R().
		SetHeaders(b.headers).
		Get(YTMusicOrigin)
	if err != nil {
		return "", fmt.Errorf("fetch YouTube Music web config: %w", err)
	}
	if resp.StatusCode() != http.StatusOK {
		return "", fmt.Errorf("fetch YouTube Music web config: HTTP %d", resp.StatusCode())
	}

	match := innertubeAPIKeyRE.FindSubmatch(resp.Body())
	if len(match) < 2 {
		return "", fmt.Errorf("YouTube Music Innertube API key not found in web config")
	}
	return string(match[1]), nil
}

func (b *YTMusicBackend) post(endpoint string, body map[string]interface{}) (map[string]interface{}, error) {
	apiKey, err := b.innertubeAPIKey()
	if err != nil {
		return nil, err
	}
	url := YTMusicBaseAPI + endpoint + "?alt=json&key=" + apiKey

	// Add context to body
	if _, ok := body["context"]; !ok {
		body["context"] = map[string]interface{}{
			"client": map[string]interface{}{
				"clientName":    "WEB_REMIX",
				"clientVersion": "1.20240101.01.00",
			},
		}
	}

	resp, err := b.httpClient.R().
		SetHeaders(b.headers).
		SetBody(body).
		Post(url)
	if err != nil {
		return nil, err
	}

	if resp.StatusCode() != http.StatusOK {
		return nil, fmt.Errorf("YTMusic API HTTP %d: %s", resp.StatusCode(), resp.String())
	}

	var result map[string]interface{}
	if err := json.Unmarshal(resp.Body(), &result); err != nil {
		return nil, err
	}
	return result, nil
}

func (b *YTMusicBackend) LikedTracks(verbose bool) ([]Track, error) {
	if verbose {
		fmt.Println("Fetching YouTube Music liked songs...")
	}

	payload := map[string]interface{}{
		"browseId": "FLLVCYTI9TWoA4JPNoX0S4A",
	}

	result, err := b.post("browse", payload)
	if err != nil {
		return nil, err
	}

	// Parsing the liked songs response is complex as it's a deep JSON.
	// For simplicity, I'll extract tracks from the common locations.
	var tracks []Track
	extractYTMTracks(result, &tracks)

	if verbose {
		fmt.Printf("  Finished fetching %d tracks from YouTube Music\n", len(tracks))
	}
	return tracks, nil
}

func extractYTMTracks(v interface{}, tracks *[]Track) {
	switch val := v.(type) {
	case map[string]interface{}:
		if videoID, ok := val["videoId"].(string); ok {
			title := ""
			if t, ok := val["title"].(map[string]interface{}); ok {
				if runs, ok := t["runs"].([]interface{}); ok && len(runs) > 0 {
					title = runs[0].(map[string]interface{})["text"].(string)
				}
			}
			var artists []string
			if a, ok := val["longBylineText"].(map[string]interface{}); ok {
				if runs, ok := a["runs"].([]interface{}); ok {
					for _, run := range runs {
						runMap := run.(map[string]interface{})
						if _, hasNav := runMap["navigationEndpoint"]; hasNav {
							artists = append(artists, runMap["text"].(string))
						}
					}
				}
			}
			if title != "" {
				*tracks = append(*tracks, Track{
					Title:    title,
					Artists:  artists,
					SourceID: videoID,
				})
			}
			return
		}
		for _, child := range val {
			extractYTMTracks(child, tracks)
		}
	case []interface{}:
		for _, child := range val {
			extractYTMTracks(child, tracks)
		}
	}
}

func (b *YTMusicBackend) SearchTrack(wanted Track, limit int) ([]Track, error) {
	query := fmt.Sprintf("%s %s", wanted.Title, strings.Join(wanted.Artists, " "))
	payload := map[string]interface{}{
		"query":  query,
		"params": "EgWKAQIIAWoKEAMQBBAJEAoQCg==", // filter for songs
	}

	result, err := b.post("search", payload)
	if err != nil {
		return nil, err
	}

	var tracks []Track
	extractYTMTracks(result, &tracks)
	if len(tracks) > limit {
		tracks = tracks[:limit]
	}
	return tracks, nil
}

func (b *YTMusicBackend) LikeTracks(tracks []Track, batchSize int, batchDelay float64, verbose bool) error {
	if verbose {
		fmt.Printf("Liking %d tracks on YouTube Music...\n", len(tracks))
	}

	for i, t := range tracks {
		if verbose {
			fmt.Printf("  [LIKE] %s\n", t.SourceID)
		}
		payload := map[string]interface{}{
			"target": map[string]interface{}{
				"videoId": t.SourceID,
			},
			"rating": "LIKE",
		}
		if _, err := b.post("like/like", payload); err != nil {
			return err
		}

		if batchDelay > 0 && i < len(tracks)-1 && (i+1)%batchSize == 0 {
			time.Sleep(time.Duration(batchDelay * float64(time.Second)))
		}
	}
	return nil
}

func EnsureYTBrowserAuth(sessionDir, lockFile string, headless bool, timeout float64) (map[string]string, error) {
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

	hasCookie := func() bool {
		cookies, _ := context.Cookies(YTMusicOrigin)
		for _, c := range cookies {
			if c.Name == YTMusicRequiredCookie {
				return true
			}
		}
		return false
	}

	if !hasCookie() {
		fmt.Fprintf(os.Stderr, "YouTube Music login required. Complete login in the opened browser window; this browser profile will be reused on future runs.\n")
		if _, err := page.Goto(YTMusicOrigin); err != nil {
			return nil, err
		}
	}

	deadline := time.Now().Add(time.Duration(timeout) * time.Second)
	for !hasCookie() && time.Now().Before(deadline) {
		time.Sleep(2 * time.Second)
	}

	if !hasCookie() {
		return nil, fmt.Errorf("YouTube Music browser session is not logged in; missing %s", YTMusicRequiredCookie)
	}

	cookies, _ := context.Cookies(YTMusicOrigin)
	cookieHeader := buildCookieHeader(cookies)
	userAgent, _ := page.Evaluate("navigator.userAgent")

	headers, err := buildYTBrowserAuthHeaders(cookieHeader, userAgent.(string))
	if err != nil {
		return nil, err
	}

	fmt.Fprintf(os.Stderr, "YouTube Music browser session auth refreshed from persistent session: %s\n", sessionDir)
	return headers, nil
}

func buildCookieHeader(cookies []playwright.Cookie) string {
	var pairs []string
	for _, c := range cookies {
		pairs = append(pairs, fmt.Sprintf("%s=%s", c.Name, c.Value))
	}
	return strings.Join(pairs, "; ")
}

func buildYTBrowserAuthHeaders(cookieHeader, userAgent string) (map[string]string, error) {
	sapisid := ""
	for _, pair := range strings.Split(cookieHeader, "; ") {
		parts := strings.SplitN(pair, "=", 2)
		if len(parts) == 2 && strings.TrimSpace(parts[0]) == YTMusicRequiredCookie {
			sapisid = strings.TrimSpace(parts[1])
			break
		}
	}
	if sapisid == "" {
		return nil, fmt.Errorf("missing %s cookie", YTMusicRequiredCookie)
	}

	now := time.Now().Unix()
	hash := sha1.New()
	hash.Write([]byte(fmt.Sprintf("%d %s %s", now, sapisid, YTMusicOrigin)))
	digest := hex.EncodeToString(hash.Sum(nil))
	authorization := fmt.Sprintf("SAPISIDHASH %d_%s", now, digest)

	return map[string]string{
		"accept":          "*/*",
		"content-type":    "application/json",
		"origin":          YTMusicOrigin,
		"user-agent":      userAgent,
		"cookie":          cookieHeader,
		"authorization":   authorization,
		"x-goog-authuser": "0",
	}, nil
}
