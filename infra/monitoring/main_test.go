package main

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"learning/platform"
)

type jwksTransport []byte

func (j jwksTransport) RoundTrip(*http.Request) (*http.Response, error) {
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(bytes.NewReader(j))}, nil
}

func TestMonitoringHealthIsPrivateAndTracksCollection(t *testing.T) {
	collector := newCollector(nil, nil, nil)
	handler := (monitorAPI{collector: collector}).handler(&platform.Authenticator{})
	check := func(path string, want int) {
		t.Helper()
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		if w.Code != want {
			t.Fatalf("%s: want %d, got %d", path, want, w.Code)
		}
	}
	check("/health/live", http.StatusOK)
	check("/health/ready", http.StatusServiceUnavailable)
	collector.collectedAt = time.Now()
	check("/health/ready", http.StatusOK)
	collector.collectedAt = time.Now().Add(-3 * collectionInterval)
	check("/health/ready", http.StatusServiceUnavailable)
}

func TestMonitoringRequiresDedicatedOperator(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	const issuer = "https://auth.example.test/realms/learning"
	jwks, err := json.Marshal(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
		{Key: &key.PublicKey, KeyID: "monitor-test", Algorithm: "RS256", Use: "sig"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("OIDC_ISSUER", issuer)
	t.Setenv("OIDC_JWKS_URL", issuer+"/certs")
	t.Setenv("OIDC_AUDIENCE", "learning")
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithHeader("kid", "monitor-test"))
	if err != nil {
		t.Fatal(err)
	}
	api := monitorAPI{collector: newCollector(nil, nil, nil)}
	handler := api.handler(platform.NewAuthenticator(&http.Client{Transport: jwksTransport(jwks)}))
	for _, tc := range []struct {
		name, audience string
		roles          []string
		expires        time.Duration
		want           int
	}{
		{"missing", "", nil, 0, http.StatusUnauthorized},
		{"expired", "learning", []string{"platform-operator"}, -time.Hour, http.StatusUnauthorized},
		{"wrong audience", "other", []string{"platform-operator"}, time.Hour, http.StatusUnauthorized},
		{"teacher", "learning", []string{"teacher"}, time.Hour, http.StatusForbidden},
		{"student", "learning", []string{"student"}, time.Hour, http.StatusForbidden},
		{"reader", "learning", []string{platform.RoleMonitoringReader}, time.Hour, http.StatusForbidden},
		{"AI client", "learning", []string{"ai-client"}, time.Hour, http.StatusForbidden},
		{"teacher operator", "learning", []string{"teacher", "platform-operator"}, time.Hour, http.StatusForbidden},
		{"student operator", "learning", []string{"student", "platform-operator"}, time.Hour, http.StatusForbidden},
		{"reader operator", "learning", []string{platform.RoleMonitoringReader, "platform-operator"}, time.Hour, http.StatusForbidden},
		{"AI client operator", "learning", []string{"ai-client", "platform-operator"}, time.Hour, http.StatusForbidden},
		{"operator", "learning", []string{"platform-operator"}, time.Hour, http.StatusOK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			paths := []string{"/api/monitoring/status"}
			if tc.want != http.StatusOK {
				paths = append(paths, "/api/monitoring/ai-spend", "/api/monitoring/ai-spend/users")
			}
			for _, path := range paths {
				r := httptest.NewRequest(http.MethodGet, path, nil)
				r.Header.Set("X-User-ID", "operator")
				if tc.audience != "" {
					token, err := jwt.Signed(signer).Claims(map[string]any{
						"sub": "test", "org_id": "org", "aud": tc.audience, "iss": issuer,
						"exp": time.Now().Add(tc.expires).Unix(), "iat": time.Now().Unix(),
						"realm_access": map[string]any{"roles": tc.roles},
					}).Serialize()
					if err != nil {
						t.Fatal(err)
					}
					r.Header.Set("Authorization", "Bearer "+token)
				}
				w := httptest.NewRecorder()
				handler.ServeHTTP(w, r)
				if w.Code != tc.want || w.Header().Get("Cache-Control") != "no-store" {
					t.Fatalf("%s: got %d and %v (%s)", path, w.Code, w.Header(), w.Body.String())
				}
				if tc.want == http.StatusOK {
					var snapshot statusSnapshot
					if json.Unmarshal(w.Body.Bytes(), &snapshot) != nil || len(snapshot.Services) != 10 || len(snapshot.DeadLetters) != 9 {
						t.Fatalf("invalid status response %s", w.Body.String())
					}
				}
			}
		})
	}
}

func TestBilledAccountProxyRetriesAndAuditsServedReports(t *testing.T) {
	for _, tc := range []struct {
		name         string
		query        string
		upstream     int
		want         int
		wantRequests int
		code         string
	}{
		{"recovered token", "?user_id=billed-account", http.StatusOK, http.StatusOK, 2, ""},
		{"two rejected tokens", "", http.StatusUnauthorized, http.StatusBadGateway, 2, "MONITORING_READER_CONFIGURATION"},
		{"missing role", "", http.StatusForbidden, http.StatusBadGateway, 1, "MONITORING_READER_CONFIGURATION"},
		{"report failure", "", http.StatusInternalServerError, http.StatusBadGateway, 1, "SPEND_REPORT_FAILED"},
		{"dependency failure", "", http.StatusServiceUnavailable, http.StatusServiceUnavailable, 1, "DEPENDENCY_UNAVAILABLE"},
		{"invalid filter", "?period=invalid", http.StatusUnprocessableEntity, http.StatusUnprocessableEntity, 1, "VALIDATION_ERROR"},
		{"duplicate filter", "?user_id=first&user_id=second", http.StatusOK, http.StatusUnprocessableEntity, 0, "VALIDATION_ERROR"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var tokens atomic.Int32
			var requests int
			const correlation = "160f395a-6d35-4e19-a0c6-109694aefed4"
			var logs bytes.Buffer
			previous := slog.Default()
			slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
			t.Cleanup(func() { slog.SetDefault(previous) })
			client := &http.Client{Transport: testHTTPTransport(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/token" {
					_, _ = fmt.Fprintf(w, `{"access_token":"token-%d","expires_in":300}`, tokens.Add(1))
					return
				}
				requests++
				if r.Header.Get("X-Correlation-ID") != correlation ||
					r.Header.Get("Authorization") != fmt.Sprintf("Bearer token-%d", requests) ||
					r.URL.RawQuery != strings.TrimPrefix(tc.query, "?") {
					t.Error("upstream request lost its query, correlation id or refreshed bearer token")
				}
				if tc.upstream == http.StatusOK && requests == 1 {
					w.WriteHeader(http.StatusUnauthorized)
					return
				}
				w.WriteHeader(tc.upstream)
				_, _ = w.Write([]byte(`{"items":[]}`))
			})}
			token := &platform.ServiceToken{URL: "http://idp.test/token", Client: client}
			api := monitorAPI{client: client, token: token, aiCore: "http://ai-core.test"}
			r := httptest.NewRequest(http.MethodGet, "/api/monitoring/ai-spend/users"+tc.query, nil)
			r.Header.Set("X-Correlation-ID", correlation)
			r = r.WithContext(platform.WithClaims(r.Context(), platform.Claims{Subject: "operator-id"}))
			w := httptest.NewRecorder()
			api.spend("users", true)(w, r)
			if w.Code != tc.want || requests != tc.wantRequests {
				t.Fatalf("got status %d and %d upstream requests, want %d and %d", w.Code, requests, tc.want, tc.wantRequests)
			}
			if tc.code != "" && !strings.Contains(w.Body.String(), `"code":"`+tc.code+`"`) {
				t.Fatalf("expected error code %q, got %s", tc.code, w.Body.String())
			}
			if tc.want == http.StatusUnprocessableEntity {
				if logs.Len() != 0 {
					t.Fatalf("unexpected log entry: %s", logs.String())
				}
				return
			}
			var entry map[string]any
			if err := json.Unmarshal(logs.Bytes(), &entry); err != nil || entry["correlation_id"] != correlation ||
				strings.Contains(logs.String(), "billed-account") {
				t.Fatalf("incorrect log entry: %s", logs.String())
			}
			if tc.want != http.StatusOK {
				if entry["msg"] != "monitoring_spend_upstream_failed" || entry["upstream_status"] != float64(tc.upstream) {
					t.Fatalf("incorrect failure entry: %s", logs.String())
				}
			} else if entry["msg"] != "monitoring_billed_accounts_read" || entry["operator_id"] != "operator-id" {
				t.Fatalf("incorrect audit entry: %s", logs.String())
			}
		})
	}
}
