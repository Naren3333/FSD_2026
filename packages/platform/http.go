// Package platform contains transport and operational conventions, never domain models.
package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"slices"
	"strconv"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Claims are the verified token claims services use: the user, their organization and realm roles.
type Claims struct {
	Subject  string `json:"sub"`
	Org      string `json:"org_id"`
	Username string `json:"preferred_username"`
	Name     string `json:"name"`
	Realm    struct {
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

// ErrorWith writes the error envelope with extra fields beside code and message, such as when a limit resets.
func ErrorWith(w http.ResponseWriter, status int, code, message string, extra map[string]any) {
	body := map[string]any{"code": code, "message": message}
	for k, v := range extra {
		body[k] = v
	}
	JSON(w, status, map[string]any{"error": body})
}

// NotFound answers 404 NOT_FOUND.
func NotFound(w http.ResponseWriter, message string) {
	Error(w, http.StatusNotFound, "NOT_FOUND", message)
}

// Invalid answers 422 VALIDATION_ERROR: the request does not meet the contract.
func Invalid(w http.ResponseWriter, message string) {
	Error(w, http.StatusUnprocessableEntity, "VALIDATION_ERROR", message)
}

// Forbidden answers 403 FORBIDDEN.
func Forbidden(w http.ResponseWriter, message string) {
	Error(w, http.StatusForbidden, "FORBIDDEN", message)
}

// Unavailable answers 503 DEPENDENCY_UNAVAILABLE: another service or store this one needs is down.
func Unavailable(w http.ResponseWriter, message string) {
	Error(w, http.StatusServiceUnavailable, "DEPENDENCY_UNAVAILABLE", message)
}

// Unauthenticated answers 401 UNAUTHENTICATED.
func Unauthenticated(w http.ResponseWriter, message string) {
	Error(w, http.StatusUnauthorized, "UNAUTHENTICATED", message)
}

// Decode reads one JSON value of at most 2 MiB into target, rejecting unknown fields. On failure it has already
// written the response: 413 for a body over the limit, 422 otherwise.
func Decode(w http.ResponseWriter, r *http.Request, target any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	d := json.NewDecoder(r.Body)
	d.DisallowUnknownFields()
	if err := d.Decode(target); err != nil {
		decodeFailed(w, err, "Request body does not match the contract.")
		return false
	}
	if err := d.Decode(new(any)); err != io.EOF {
		decodeFailed(w, err, "Expected one JSON value.")
		return false
	}
	return true
}

func decodeFailed(w http.ResponseWriter, err error, message string) {
	var tooBig *http.MaxBytesError
	if errors.As(err, &tooBig) {
		Error(w, http.StatusRequestEntityTooLarge, "PAYLOAD_TOO_LARGE", "Request body must be at most 2 MiB.")
		return
	}
	Invalid(w, message)
}

// ID reports whether value is a UUID.
func ID(value string) bool { _, err := uuid.Parse(value); return err == nil }

// Page reads a list's ?offset= and ?limit= query values: offset defaults to 0 and limit to fallback, and limit must
// be 1–most. On failure it has written a 422.
func Page(w http.ResponseWriter, r *http.Request, fallback, most int) (offset, limit int, ok bool) {
	query := r.URL.Query()
	offset, okOffset := whole(query.Get("offset"), 0)
	limit, okLimit := whole(query.Get("limit"), fallback)
	if !okOffset || !okLimit || limit < 1 || limit > most {
		Invalid(w, fmt.Sprintf("offset must be 0 or more and limit between 1 and %d.", most))
		return 0, 0, false
	}
	return offset, limit, true
}

// whole parses a non-negative whole-number query value, or gives fallback when it is absent.
func whole(value string, fallback int) (int, bool) {
	if value == "" {
		return fallback, true
	}
	n, err := strconv.Atoi(value)
	return n, err == nil && n >= 0
}

// Teacher requires the token's teacher role, writing a 403 otherwise. Classroom-specific access needs TeacherOf.
func Teacher(w http.ResponseWriter, r *http.Request) bool {
	if !Actor(r).Has("teacher") {
		Forbidden(w, "Teacher access is required.")
		return false
	}
	return true
}

// Fail logs an unexpected error and answers 500 without exposing it.
func Fail(w http.ResponseWriter, err error) {
	slog.Error("operation_failed", "error_category", fmt.Sprintf("%T", err), "error", err.Error())
	Error(w, http.StatusInternalServerError, "INTERNAL_ERROR", "The operation could not be completed.")
}

// ErrNotFound is what a store returns when the row asked for is absent. It is pgx.ErrNoRows, so a QueryRow(...).Scan
// result passes through unchanged, and handlers and test doubles need not import pgx to name it.
var ErrNotFound = pgx.ErrNoRows

// Found reports whether a lookup succeeded. Otherwise it has written the response: 404 with message when err is
// ErrNotFound, 500 for any other error.
func Found(w http.ResponseWriter, err error, message string) bool {
	switch {
	case err == nil:
		return true
	case errors.Is(err, ErrNotFound):
		NotFound(w, message)
	default:
		Fail(w, err)
	}
	return false
}

// Request builds a call to another service carrying ctx's correlation id.
func Request(ctx context.Context, method, target string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequestWithContext(ctx, method, target, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("X-Correlation-ID", Correlation(ctx))
	return req, nil
}

// Forward builds a body-less call to another service on behalf of r's caller: it carries the caller's own bearer
// token, which the other service verifies again, and r's correlation id.
func Forward(r *http.Request, method, target string) (*http.Request, error) {
	req, err := Request(r.Context(), method, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	return req, nil
}
