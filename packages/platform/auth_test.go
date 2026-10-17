package platform

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
)

func TestRealJWTValidation(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	impostor, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	jwks := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "test", Algorithm: "RS256", Use: "sig"}}})
	}))
	defer jwks.Close()
	issuer := "https://identity.example.test/realms/learning"
	t.Setenv("OIDC_ISSUER", issuer)
	t.Setenv("OIDC_JWKS_URL", jwks.URL)
	t.Setenv("OIDC_AUDIENCE", "learning")
	a := &App{Mux: http.NewServeMux(), auth: NewAuthenticator(jwks.Client())}
	a.Mux.HandleFunc("GET /private", func(w http.ResponseWriter, r *http.Request) {
		JSON(w, 200, map[string]string{"subject": Actor(r).Subject})
	})
	publicDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		t.Fatal(err)
	}
	publicPEM := pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicDER})
	sign := func(alg jose.SignatureAlgorithm, signingKey any, kid string, claims map[string]any) string {
		signer, err := jose.NewSigner(jose.SigningKey{Algorithm: alg, Key: signingKey}, (&jose.SignerOptions{}).WithHeader("kid", kid))
		if err != nil {
			t.Fatal(err)
		}
		token, err := jwt.Signed(signer).Claims(claims).Serialize()
		if err != nil {
			t.Fatal(err)
		}
		return token
	}
	claims := func(edit func(map[string]any)) map[string]any {
		c := map[string]any{"sub": "test-student", "org_id": "test-org", "aud": "learning", "iss": issuer, "exp": time.Now().Add(time.Hour).Unix(), "iat": time.Now().Unix(), "realm_access": map[string]any{"roles": []string{"student"}}}
		if edit != nil {
			edit(c)
		}
		return c
	}
	valid := func(edit func(map[string]any)) string { return "Bearer " + sign(jose.RS256, key, "test", claims(edit)) }
	roles := func(names ...string) func(map[string]any) {
		return func(c map[string]any) { c["realm_access"] = map[string]any{"roles": names} }
	}
	encode := func(v any) string {
		b, _ := json.Marshal(v)
		return base64.RawURLEncoding.EncodeToString(b)
	}
	for _, tc := range []struct {
		name, authorization string
		status              int
		code                string
	}{
		{"valid", valid(nil), 200, ""},
		{"wrong audience", valid(func(c map[string]any) { c["aud"] = "other" }), 401, "UNAUTHENTICATED"},
		{"wrong issuer", valid(func(c map[string]any) { c["iss"] = "https://evil.test" }), 401, "UNAUTHENTICATED"},
		{"expired", valid(func(c map[string]any) { c["exp"] = time.Now().Add(-time.Hour).Unix() }), 401, "UNAUTHENTICATED"},
		{"alg none", "Bearer " + encode(map[string]string{"alg": "none", "kid": "test"}) + "." + encode(claims(nil)) + ".", 401, "UNAUTHENTICATED"},
		{"HS256 keyed with the public key", "Bearer " + sign(jose.HS256, publicPEM, "test", claims(nil)), 401, "UNAUTHENTICATED"},
		{"another key with the same kid", "Bearer " + sign(jose.RS256, impostor, "test", claims(nil)), 401, "UNAUTHENTICATED"},
		{"unknown kid", "Bearer " + sign(jose.RS256, key, "other", claims(nil)), 401, "UNAUTHENTICATED"},
		{"missing sub", valid(func(c map[string]any) { delete(c, "sub") }), 401, "UNAUTHENTICATED"},
		{"missing org_id", valid(func(c map[string]any) { delete(c, "org_id") }), 401, "UNAUTHENTICATED"},
		{"lowercase bearer", "bearer " + strings.TrimPrefix(valid(nil), "Bearer "), 401, "UNAUTHENTICATED"},
		{"teacher and student", valid(roles("teacher", "student")), 403, "ROLE_CONFLICT"},
		{"teacher and ai-client", valid(roles("teacher", "ai-client")), 200, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest(http.MethodGet, "/private", nil)
			r.Header.Set("Authorization", tc.authorization)
			w := httptest.NewRecorder()
			a.Handler().ServeHTTP(w, r)
			var body struct{ Error struct{ Code string } }
			_ = json.Unmarshal(w.Body.Bytes(), &body)
			if w.Code != tc.status || body.Error.Code != tc.code {
				t.Fatalf("status %d, code %q", w.Code, body.Error.Code)
			}
		})
	}
	r := httptest.NewRequest(http.MethodGet, "/private", nil)
	r.Header.Set("X-User-ID", "test-student")
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, r)
	if w.Code != 401 {
		t.Fatal("trusted arbitrary identity header")
	}
}
