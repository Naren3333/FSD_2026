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

	"golang.org/x/sync/singleflight"
)

// fetchtimeout bounds one token fetch, which runs apart from its callers' contexts (see get).
const fetchTimeout = 10 * time.Second

// servicetoken is a service account's client-credentials access token, cached until shortly before it
// expires.
type ServiceToken struct {
	URL, ID, Secret string
	Client          *http.Client
	mu              sync.Mutex
	token           string
	expires         time.Time
	fetches         singleflight.Group
}

// get returns the cached token, or fetches a new one from the token endpoint.
func (t *ServiceToken) Get(ctx context.Context) (string, error) {
	t.mu.Lock()
	token, valid := t.token, t.token != "" && time.Now().Before(t.expires)
	t.mu.Unlock()
	if valid {
		return token, nil
	}
	result := t.fetches.DoChan("token", func() (any, error) {
		fetchCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), fetchTimeout)
		defer cancel()
		token, expires, err := t.fetch(fetchCtx)
		if err != nil {
			return "", err
		}
		t.mu.Lock()
		t.token, t.expires = token, expires
		t.mu.Unlock()
		return token, nil
	})
	select {
	case r := <-result:
		token, _ := r.Val.(string)
		return token, r.Err
	case <-ctx.Done():
		return "", ctx.Err()
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
	// renewed 30 seconds early, or halfway through a token that lives a minute or less, so a short-lived token
	// is still reused rather than fetched again on every call.
	lifetime := time.Duration(body.ExpiresIn) * time.Second
	return body.AccessToken, time.Now().Add(lifetime - min(30*time.Second, lifetime/2)), nil
}

// reset drops the cached token, so the next get fetches a new one (after the token was refused, say).
func (t *ServiceToken) Reset() {
	t.mu.Lock()
	t.token = ""
	t.mu.Unlock()
}
