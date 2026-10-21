package platform

import (
	"context"
	"net/http"
	"strings"

	"github.com/coreos/go-oidc/v3/oidc"
)

// authenticator verifies bearer tokens: rs256 signature against the realm's keys, issuer, audience and
// expiry, and the presence of the subject and organization claims.
type Authenticator struct {
	verifier *oidc.IDTokenVerifier
}

// newauthenticator reads oidc_issuer, oidc_jwks_url and oidc_audience.
func NewAuthenticator(client *http.Client) *Authenticator {
	ctx := oidc.ClientContext(context.Background(), client)
	keys := oidc.NewRemoteKeySet(ctx, Required("OIDC_JWKS_URL"))
	config := &oidc.Config{ClientID: Required("OIDC_AUDIENCE"), SupportedSigningAlgs: []string{"RS256"}}
	return &Authenticator{oidc.NewVerifier(Required("OIDC_ISSUER"), keys, config)}
}

// wrap passes requests with a valid bearer token to next, with the verified claims in the context (see
// actor), and answers everything else 401.
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
		if c.Has("teacher") && c.Has("student") {
			RoleConflict(w)
			return
		}
		next.ServeHTTP(w, r.WithContext(WithClaims(r.Context(), c)))
	})
}

// roleconflict answers 403 role_conflict: an account holds both the teacher and the student role, which no
// service accepts.
func RoleConflict(w http.ResponseWriter) {
	Error(w, http.StatusForbidden, "ROLE_CONFLICT", "An account must be either a teacher or a student, not both.")
}
