// package main runs the private platform monitoring collector and read api.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/segmentio/kafka-go"
	"learning/platform"
)

type monitorAPI struct {
	collector *collector
	client    *http.Client
	token     *platform.ServiceToken
	aiCore    string
}

func (api monitorAPI) handler(auth *platform.Authenticator) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/monitoring/status", func(w http.ResponseWriter, _ *http.Request) {
		platform.JSON(w, http.StatusOK, api.collector.snapshot())
	})
	mux.HandleFunc("GET /api/monitoring/ai-spend", api.spend("summary", false))
	mux.HandleFunc("GET /api/monitoring/ai-spend/users", api.spend("users", true))
	authorized := auth.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		actor := platform.Actor(r)
		if !isOperator(actor) {
			platform.Forbidden(w, "A school admin account is required.")
			return
		}
		mux.ServeHTTP(w, r)
	}))
	root := http.NewServeMux()
	root.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) {
		platform.JSON(w, http.StatusOK, map[string]string{"status": "alive"})
	})
	root.HandleFunc("GET /health/ready", func(w http.ResponseWriter, _ *http.Request) {
		if !api.collector.ready() {
			platform.Unavailable(w, "Waiting for a monitoring collection cycle.")
			return
		}
		platform.JSON(w, http.StatusOK, map[string]string{"status": "ready"})
	})
	root.Handle("/", authorized)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		root.ServeHTTP(w, r)
	})
}

// spend proxies one of ai-core's spend reports, always narrowed to the admin's own school (adr 0019).
func (api monitorAPI) spend(endpoint string, users bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		org := platform.Actor(r).Org
		ctx, cancel := context.WithTimeout(r.Context(), 6*time.Second)
		defer cancel()
		correlation := r.Header.Get("X-Correlation-ID")
		if !platform.ID(correlation) {
			correlation = platform.Correlation(ctx)
		}
		w.Header().Set("X-Correlation-ID", correlation)
		query, ok := spendQuery(w, r, org, users)
		if !ok {
			return
		}
		target := api.aiCore + "/internal/spend/" + endpoint + "?" + query.Encode()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			platform.Unavailable(w, "AI expenditure is temporarily unavailable.")
			return
		}
		req.Header.Set("X-Correlation-ID", correlation)
		res, err := monitoringGet(api.client, api.token, req)
		if err != nil {
			platform.Unavailable(w, "AI expenditure is temporarily unavailable.")
			return
		}
		defer res.Body.Close()
		if res.StatusCode != http.StatusOK {
			writeSpendUpstreamError(w, res.StatusCode, endpoint, correlation)
			return
		}
		const limit = 128 << 10
		body, err := io.ReadAll(io.LimitReader(res.Body, limit+1))
		if err != nil {
			platform.Unavailable(w, "AI expenditure is temporarily unavailable.")
			return
		}
		if len(body) > limit || !json.Valid(body) {
			slog.Warn("monitoring_spend_response_invalid", "endpoint", endpoint, "upstream_status", res.StatusCode, "correlation_id", correlation)
			platform.Error(w, http.StatusBadGateway, "SPEND_REPORT_FAILED", "AI expenditure could not be retrieved from ai-core.")
			return
		}
		w.Header().Set("Content-Type", "application/json")
		if _, err := w.Write(body); err == nil && users {
			slog.Info("monitoring_billed_accounts_read", "admin_id", platform.Actor(r).Subject, "org_id", org, "correlation_id", correlation)
		}
	}
}

// spendQuery is ai-core's query for the report: the admin's own organization, refusing another one, and for
// the billed accounts each filter given once. it writes the refusal when the request has none to send.
func spendQuery(w http.ResponseWriter, r *http.Request, org string, users bool) (url.Values, bool) {
	if values, present := r.URL.Query()["org_id"]; present && (len(values) != 1 || values[0] != org) {
		platform.Forbidden(w, "AI expenditure is limited to your own school.")
		return nil, false
	}
	query := url.Values{"org_id": {org}}
	if !users {
		return query, true
	}
	for _, name := range []string{"period", "user_id", "offset"} {
		if values, present := r.URL.Query()[name]; present {
			if len(values) != 1 {
				platform.Invalid(w, "Supply each spend filter once.")
				return nil, false
			}
			query.Set(name, values[0])
		}
	}
	return query, true
}

func writeSpendUpstreamError(w http.ResponseWriter, status int, endpoint, correlation string) {
	if status == http.StatusUnprocessableEntity {
		platform.Invalid(w, "Check the spend filters and try again.")
		return
	}
	slog.Warn("monitoring_spend_upstream_failed", "endpoint", endpoint, "upstream_status", status, "correlation_id", correlation)
	switch status {
	case http.StatusUnauthorized, http.StatusForbidden:
		platform.Error(w, http.StatusBadGateway, "MONITORING_READER_CONFIGURATION", "AI expenditure access is misconfigured. Check the monitoring service account.")
	case http.StatusServiceUnavailable, http.StatusGatewayTimeout:
		platform.Unavailable(w, "AI expenditure is temporarily unavailable.")
	default:
		platform.Error(w, http.StatusBadGateway, "SPEND_REPORT_FAILED", "AI expenditure could not be retrieved from ai-core.")
	}
}

// monitoringget retries a read once after an upstream rejects its service token.
func monitoringGet(client *http.Client, tokens *platform.ServiceToken, req *http.Request) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		token, err := tokens.Get(req.Context())
		if err != nil {
			return nil, err
		}
		request := req.Clone(req.Context())
		request.Header.Set("Authorization", "Bearer "+token)
		res, err := client.Do(request)
		if err != nil {
			return nil, err
		}
		if res.StatusCode != http.StatusUnauthorized {
			return res, nil
		}
		tokens.Reject(token)
		if attempt == 1 {
			return res, nil
		}
		_ = res.Body.Close()
	}
}

func main() {
	platform.Healthcheck()
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", "monitoring"))
	client := &http.Client{
		Timeout: 5 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	token := &platform.ServiceToken{
		URL: platform.Required("MONITORING_TOKEN_URL"), ID: "monitoring-reader",
		Secret: platform.Required("MONITORING_READER_CLIENT_SECRET"), Client: client,
	}
	broker := &kafka.Client{
		Addr: kafka.TCP(strings.Split(platform.Required("KAFKA_BROKERS"), ",")...), Timeout: 4 * time.Second,
	}
	collector := newCollector(client, token, broker)
	api := monitorAPI{collector: collector, client: client, token: token, aiCore: "http://ai-core:8080"}
	server := &http.Server{
		Addr: ":" + platform.Env("PORT", "8080"), Handler: api.handler(platform.NewAuthenticator(client)),
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 8 * time.Second, WriteTimeout: 10 * time.Second,
		IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10,
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	collected := make(chan struct{})
	go func() { defer close(collected); collector.run(ctx) }()
	go func() {
		if err := server.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
			slog.Error("listen_failed", "error", err)
			stop()
		}
	}()
	<-ctx.Done()
	shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	_ = server.Shutdown(shutdown)
	<-collected
}
