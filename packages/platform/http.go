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
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/coreos/go-oidc/v3/oidc"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Claims struct {
	Subject string `json:"sub"`
	Org     string `json:"org_id"`
	Realm   struct {
		Roles []string `json:"roles"`
	} `json:"realm_access"`
}

func (c Claims) Has(role string) bool {
	for _, r := range c.Realm.Roles {
		if r == role {
			return true
		}
	}
	return false
}

type contextKey string

const claimsKey contextKey = "claims"
const correlationKey contextKey = "correlation"

func Actor(r *http.Request) Claims { c, _ := r.Context().Value(claimsKey).(Claims); return c }
func Correlation(ctx context.Context) string {
	v, _ := ctx.Value(correlationKey).(string)
	if v == "" {
		return uuid.NewString()
	}
	return v
}
func Required(key string) string {
	value := os.Getenv(key)
	if value == "" {
		panic("missing configuration: " + key)
	}
	return value
}
func Env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
func JSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func Error(w http.ResponseWriter, status int, code, message string) {
	JSON(w, status, map[string]any{"error": map[string]string{"code": code, "message": message}})
}
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
func ID(value string) bool { _, err := uuid.Parse(value); return err == nil }
func Teacher(w http.ResponseWriter, r *http.Request) bool {
	if !Actor(r).Has("teacher") {
		Error(w, 403, "FORBIDDEN", "Teacher access is required.")
		return false
	}
	return true
}

type App struct {
	DB       *pgxpool.Pool
	Mux      *http.ServeMux
	Name     string
	verifier *oidc.IDTokenVerifier
	Client   *http.Client
	Checks   []func(context.Context) error
}

type responseRecorder struct {
	http.ResponseWriter
	status int
}

func (w *responseRecorder) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
func (w *responseRecorder) Write(data []byte) (int, error) {
	if w.status == 0 {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(data)
}

func New(name, migration string) (*App, error) {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", name))
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	config, err := pgxpool.ParseConfig(Required("DATABASE_URL"))
	if err != nil {
		return nil, err
	}
	config.MaxConns = 10
	config.MinConns = 1
	config.MaxConnLifetime = 30 * time.Minute
	db, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, err
	}
	initialized := false
	defer func() {
		if !initialized {
			db.Close()
		}
	}()
	if err = db.Ping(ctx); err != nil {
		return nil, err
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(314159)"); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, migration); err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	issuer := Required("OIDC_ISSUER")
	client := &http.Client{Timeout: 5 * time.Second}
	authCtx := oidc.ClientContext(context.Background(), client)
	verifier := oidc.NewVerifier(issuer, oidc.NewRemoteKeySet(authCtx, Required("OIDC_JWKS_URL")), &oidc.Config{ClientID: Required("OIDC_AUDIENCE"), SupportedSigningAlgs: []string{"RS256"}})
	a := &App{DB: db, Mux: http.NewServeMux(), Name: name, verifier: verifier, Client: client}
	a.Mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, r *http.Request) { JSON(w, 200, map[string]string{"status": "alive"}) })
	a.Mux.HandleFunc("GET /health/ready", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if a.DB.Ping(ctx) != nil {
			Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Database unavailable.")
			return
		}
		for _, check := range a.Checks {
			if check(ctx) != nil {
				Error(w, 503, "DEPENDENCY_UNAVAILABLE", "An essential dependency is unavailable.")
				return
			}
		}
		JSON(w, 200, map[string]string{"status": "ready"})
	})
	initialized = true
	return a, nil
}
func (a *App) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		recorder := &responseRecorder{ResponseWriter: w}
		w = recorder
		start := time.Now()
		correlation := r.Header.Get("X-Correlation-ID")
		if !ID(correlation) {
			correlation = uuid.NewString()
		}
		request := uuid.NewString()
		w.Header().Set("X-Correlation-ID", correlation)
		w.Header().Set("X-Request-ID", request)
		defer func() {
			category := ""
			if recorder.status >= 400 {
				category = "http_error"
			}
			slog.Info("request", "request_id", request, "correlation_id", correlation, "method", r.Method, "status", recorder.status, "error_category", category, "duration_ms", time.Since(start).Milliseconds())
		}()
		ctx, cancel := context.WithTimeout(context.WithValue(r.Context(), correlationKey, correlation), 15*time.Second)
		defer cancel()
		r = r.WithContext(ctx)
		if strings.HasPrefix(r.URL.Path, "/health/") {
			a.Mux.ServeHTTP(w, r)
			return
		}
		auth := r.Header.Get("Authorization")
		if !strings.HasPrefix(auth, "Bearer ") {
			Error(w, 401, "UNAUTHENTICATED", "Sign in to continue.")
			return
		}
		token, err := a.verifier.Verify(ctx, strings.TrimPrefix(auth, "Bearer "))
		if err != nil {
			Error(w, 401, "UNAUTHENTICATED", "The session is invalid or expired.")
			return
		}
		var c Claims
		if token.Claims(&c) != nil || c.Subject == "" || c.Org == "" {
			Error(w, 401, "UNAUTHENTICATED", "Required identity claims are missing.")
			return
		}
		a.Mux.ServeHTTP(w, r.WithContext(context.WithValue(ctx, claimsKey, c)))
	})
}

// Access delegates classroom policy to its owner and forwards the verified caller token.
func (a *App) Access(w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	if !ID(classroom) {
		Error(w, 422, "VALIDATION_ERROR", "A valid classroom identifier is required.")
		return false
	}
	url := Required("IDENTITY_URL") + "/v1/classrooms/" + classroom + "/access"
	req, err := http.NewRequestWithContext(r.Context(), "GET", url, nil)
	if err != nil {
		Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Authorization unavailable.")
		return false
	}
	q := req.URL.Query()
	if student != "" {
		q.Set("student_id", student)
	}
	req.URL.RawQuery = q.Encode()
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	req.Header.Set("X-Correlation-ID", Correlation(r.Context()))
	res, err := a.Client.Do(req)
	if err != nil {
		Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Authorization unavailable; retry later.")
		return false
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		if res.StatusCode >= 500 {
			Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Authorization unavailable.")
		} else {
			Error(w, 403, "FORBIDDEN", "You do not have access to this classroom or student.")
		}
		return false
	}
	return true
}
func (a *App) Run(workers ...func(context.Context)) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	defer a.DB.Close()
	var group sync.WaitGroup
	defer group.Wait()
	defer stop()
	for _, worker := range workers {
		group.Add(1)
		go func() { defer group.Done(); worker(ctx) }()
	}
	server := &http.Server{Addr: ":" + Env("PORT", "8080"), Handler: a.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 20 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10}
	done := make(chan error, 1)
	go func() { done <- server.ListenAndServe() }()
	select {
	case err := <-done:
		if !errors.Is(err, http.ErrServerClosed) {
			return err
		}
	case <-ctx.Done():
	}
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return server.Shutdown(shutdown)
}
func Fail(w http.ResponseWriter, err error) {
	slog.Error("operation_failed", "error_category", fmt.Sprintf("%T", err))
	Error(w, 500, "INTERNAL_ERROR", "The operation could not be completed.")
}
