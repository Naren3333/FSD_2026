package platform

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestRealJWTValidation(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "test", Algorithm: "RS256", Use: "sig"}}})
	}))
	defer jwks.Close()
	issuer := "https://identity.example.test/realms/learning"
	verifier := oidc.NewVerifier(issuer, oidc.NewRemoteKeySet(context.Background(), jwks.URL), &oidc.Config{ClientID: "learning", SupportedSigningAlgs: []string{"RS256"}})
	a := &App{Mux: http.NewServeMux(), verifier: verifier}
	a.Mux.HandleFunc("GET /private", func(w http.ResponseWriter, r *http.Request) {
		JSON(w, 200, map[string]string{"subject": Actor(r).Subject})
	})
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithHeader("kid", "test"))
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name, aud, iss string
		expiry         int64
		status         int
	}{{"valid", "learning", issuer, time.Now().Add(time.Hour).Unix(), 200}, {"wrong audience", "other", issuer, time.Now().Add(time.Hour).Unix(), 401}, {"wrong issuer", "learning", "https://evil.test", time.Now().Add(time.Hour).Unix(), 401}, {"expired", "learning", issuer, time.Now().Add(-time.Hour).Unix(), 401}} {
		t.Run(tc.name, func(t *testing.T) {
			token, err := jwt.Signed(signer).Claims(map[string]any{"sub": "test-student", "org_id": "test-org", "aud": tc.aud, "iss": tc.iss, "exp": tc.expiry, "iat": time.Now().Unix()}).Serialize()
			if err != nil {
				t.Fatal(err)
			}
			r := httptest.NewRequest("GET", "/private", nil)
			r.Header.Set("Authorization", "Bearer "+token)
			w := httptest.NewRecorder()
			a.Handler().ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatalf("status %d", w.Code)
			}
		})
	}
	r := httptest.NewRequest("GET", "/private", nil)
	r.Header.Set("X-User-ID", "test-student")
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatal("trusted arbitrary identity header")
	}
}
