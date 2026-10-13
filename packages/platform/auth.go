package platform

import (
	"context"
	"net/http"
	"strings"

	"github.com/coreos/go-oidc/v3/oidc"
)

// Authenticator verifies bearer tokens: RS256 signature against the realm's keys, issuer, audience and expiry,
// and the presence of the subject and organization claims.
type Authenticator struct {
	verifier *oidc.IDTokenVerifier
}

// NewAuthenticator reads OIDC_ISSUER, OIDC_JWKS_URL and OIDC_AUDIENCE. Keys are fetched with client.
func NewAuthenticator(client *http.Client) *Authenticator {
	ctx := oidc.ClientContext(context.Background(), client)
	keys := oidc.NewRemoteKeySet(ctx, Required("OIDC_JWKS_URL"))
	config := &oidc.Config{ClientID: Required("OIDC_AUDIENCE"), SupportedSigningAlgs: []string{"RS256"}}
	return &Authenticator{oidc.NewVerifier(Required("OIDC_ISSUER"), keys, config)}
}

// Wrap passes requests with a valid bearer token to next, with the verified claims in the context (see Actor),
// and answers everything else 401.
func (a *Authenticator) Wrap(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, "Bearer ") {
			Unauthenticated(w, "Sign in to continue.")
			return
		}
		token, err := a.verifier.Verify(r.Context(), strings.TrimPrefix(auth, "Bearer "))
		if err != nil {
			Unauthenticated(w, "The session is invalid or expired.")
			return
		}
		var c Claims
		if token.Claims(&c) != nil || c.Subject == "" || c.Org == "" {
			Unauthenticated(w, "Required identity claims are missing.")
			return
		}
		next.ServeHTTP(w, r.WithContext(WithClaims(r.Context(), c)))
	})
}
