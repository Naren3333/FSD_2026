package platform

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// ServiceToken is a service account's client-credentials access token, cached until shortly before it expires.
// Services that call the private AI service use one (docs/decisions/0002-ai-integration.md).
type ServiceToken struct {
	URL, ID, Secret string
	Client          *http.Client
	mu              sync.Mutex
	token           string
	expires         time.Time
	fetching        chan struct{} // closed when the fetch in progress ends; nil when none is
}

// Get returns the cached token, or fetches a new one from the token endpoint. Only one fetch runs at a time, and the
// lock is not held during it: other callers wait for its result only as long as their own ctx allows, and fetch
// themselves if it failed.
func (t *ServiceToken) Get(ctx context.Context) (string, error) {
	for {
		t.mu.Lock()
		if t.token != "" && time.Now().Before(t.expires) {
			token := t.token
			t.mu.Unlock()
			return token, nil
		}
		if t.fetching == nil {
			done := make(chan struct{})
			t.fetching = done
			t.mu.Unlock()
			token, expires, err := t.fetch(ctx)
			t.mu.Lock()
			if err == nil {
				t.token, t.expires = token, expires
			}
			t.fetching = nil
			close(done)
			t.mu.Unlock()
			return token, err
		}
		wait := t.fetching
		t.mu.Unlock()
		select {
		case <-wait:
		case <-ctx.Done():
			return "", ctx.Err()
		}
	}
}

func (t *ServiceToken) fetch(ctx context.Context) (string, time.Time, error) {
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {t.ID}, "client_secret": {t.Secret}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.URL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", time.Time{}, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := t.Client.Do(req)
	if err != nil {
		return "", time.Time{}, err
	}
	defer res.Body.Close()
	var body struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if res.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(&body) != nil || body.AccessToken == "" {
		return "", time.Time{}, fmt.Errorf("token endpoint answered %d", res.StatusCode)
	}
	// Renewed 30 seconds early, or halfway through a token that lives a minute or less, so a short-lived token is still
	// reused rather than fetched again on every call.
	lifetime := time.Duration(body.ExpiresIn) * time.Second
	return body.AccessToken, time.Now().Add(lifetime - min(30*time.Second, lifetime/2)), nil
}

// Reset drops the cached token, so the next Get fetches a new one (after the token was refused, say).
func (t *ServiceToken) Reset() {
	t.mu.Lock()
	t.token = ""
	t.mu.Unlock()
}
