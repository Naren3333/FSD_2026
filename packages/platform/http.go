// Package platform contains transport and operational conventions, never domain models.
package platform

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"slices"

	"github.com/google/uuid"
)

// Claims are the verified token claims services use: the user, their organization and realm roles.
type Claims struct {
	Subject string `json:"sub"`
	Org     string `json:"org_id"`
	Realm   struct {
		Roles []string `json:"roles"`
	} `json:"realm_access"`
}

// Has reports whether the token carries the realm role.
func (c Claims) Has(role string) bool { return slices.Contains(c.Realm.Roles, role) }

type contextKey string

const claimsKey contextKey = "claims"
const correlationKey contextKey = "correlation"

// Actor returns the verified claims of the request's caller.
func Actor(r *http.Request) Claims { c, _ := r.Context().Value(claimsKey).(Claims); return c }

// WithClaims returns ctx carrying verified claims. The authenticator sets them after verifying the bearer token;
// handler tests use it to stand in for a verified caller. Nothing reads claims from request headers.
func WithClaims(ctx context.Context, c Claims) context.Context {
	return context.WithValue(ctx, claimsKey, c)
}

// Correlation returns the request's correlation id, or a new one outside a request.
func Correlation(ctx context.Context) string {
	v, _ := ctx.Value(correlationKey).(string)
	if v == "" {
		return uuid.NewString()
	}
	return v
}

// JSON writes value as the response body with the given status.
func JSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

// Error writes the standard error envelope {"error": {"code", "message"}}.
func Error(w http.ResponseWriter, status int, code, message string) {
	JSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": message}})
}

// Decode reads one JSON value of at most 2 MiB into target, rejecting unknown fields. On failure it has already
// written a 422 response.
func Decode(w http.ResponseWriter, r *http.Request, target any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		Error(w, 422, "VALIDATION_ERROR", "Request body does not match the contract.")
		return false
	}
	if err := d.Decode(new(any)); err != io.EOF {
		Error(w, 422, "VALIDATION_ERROR", "Expected one JSON value.")
		return false
	}
	return true
}

// ID reports whether value is a UUID.
func ID(value string) bool { _, err := uuid.Parse(value); return err == nil }

// Teacher requires the token's teacher role, writing a 403 otherwise. Classroom-specific access needs TeacherOf.
func Teacher(w http.ResponseWriter, r *http.Request) bool {
	if !Actor(r).Has("teacher") {
		Error(w, 403, "FORBIDDEN", "Teacher access is required.")
		return false
	}
	return true
}

// Fail logs an unexpected error and answers 500 without exposing it.
func Fail(w http.ResponseWriter, err error) {
	slog.Error("operation_failed", "error_category", fmt.Sprintf("%T", err), "error", err.Error())
	Error(w, 500, "INTERNAL_ERROR", "The operation could not be completed.")
}
