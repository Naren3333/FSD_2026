package platform

import (
	"context"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// App wires one service together: its database, routes, token verification, readiness checks and the background
// workers it runs. Services build their handlers on the narrower pieces (the pool, ClassroomAccess), not on App.
type App struct {
	Name   string
	DB     *pgxpool.Pool
	Mux    *http.ServeMux
	Client *http.Client // for calls to other services, with a short timeout
	Checks []func(context.Context) error
	auth   *Authenticator
}

// New opens DATABASE_URL with the service's migrations (see OpenDB), sets up token verification from the OIDC_*
// settings and registers the health routes.
func New(name string, migrations fs.FS, bootstrap string) (*App, error) {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", name))
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	db, err := OpenDB(ctx, Required("DATABASE_URL"), migrations, bootstrap)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 5 * time.Second}
	a := &App{Name: name, DB: db, Mux: http.NewServeMux(), Client: client, auth: NewAuthenticator(client)}
	a.Mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, 200, map[string]string{"status": "alive"})
	})
	a.Mux.HandleFunc("GET /health/ready", a.ready)
	return a, nil
}

func (a *App) ready(w http.ResponseWriter, r *http.Request) {
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
}

// Identity returns ClassroomAccess backed by the Identity service at IDENTITY_URL.
func (a *App) Identity() *Identity { return NewIdentity(Required("IDENTITY_URL"), a.Client) }

// Handler wraps the routes with request logging, correlation ids and a deadline; every route except /health/
// also needs a verified bearer token.
func (a *App) Handler() http.Handler {
	authenticated := a.auth.Wrap(a.Mux)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		recorder := &responseRecorder{ResponseWriter: w}
		start := time.Now()
		correlation := r.Header.Get("X-Correlation-ID")
		if !ID(correlation) {
			correlation = uuid.NewString()
		}
		request := uuid.NewString()
		recorder.Header().Set("X-Correlation-ID", correlation)
		recorder.Header().Set("X-Request-ID", request)
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
			a.Mux.ServeHTTP(recorder, r)
			return
		}
		authenticated.ServeHTTP(recorder, r)
	})
}

// Run serves HTTP and runs the workers until SIGINT or SIGTERM, then shuts down gracefully: the server stops
// taking requests, the workers' context is cancelled, and the pool closes once they have returned.
func (a *App) Run(workers ...func(context.Context)) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
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

// Serve builds the App, lets wire register the service's routes and return its background workers, and runs it
// until shutdown. Any failure is logged and exits the process with status 1.
func Serve(name string, migrations fs.FS, bootstrap string, wire func(a *App) ([]func(context.Context), error)) {
	a, err := New(name, migrations, bootstrap)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	workers, err := wire(a)
	if err != nil {
		a.DB.Close()
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	if err = a.Run(workers...); err != nil {
		slog.Error("server_failed", "error", err.Error())
		os.Exit(1)
	}
}
