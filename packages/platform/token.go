package platform

import (
	"context"
	"encoding/json"
	"fmt"
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
}

// Get returns the cached token, or fetches a new one from the token endpoint.
func (t *ServiceToken) Get(ctx context.Context) (string, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.token != "" && time.Now().Before(t.expires) {
		return t.token, nil
	}
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {t.ID}, "client_secret": {t.Secret}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, t.URL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := t.Client.Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	var body struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if res.StatusCode != http.StatusOK || json.NewDecoder(res.Body).Decode(&body) != nil || body.AccessToken == "" {
		return "", fmt.Errorf("token endpoint answered %d", res.StatusCode)
	}
	t.token, t.expires = body.AccessToken, time.Now().Add(time.Duration(body.ExpiresIn)*time.Second-30*time.Second)
	return t.token, nil
}

// Reset drops the cached token, so the next Get fetches a new one (after the token was refused, say).
func (t *ServiceToken) Reset() {
	t.mu.Lock()
	t.token = ""
	t.mu.Unlock()
}
