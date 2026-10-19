package platform

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"sync"
	"time"
)

var operational = struct {
	sync.Mutex
	values map[string]float64
}{values: map[string]float64{}}

// CountMetric uses fixed names supplied by code, never user/resource/query labels.
func CountMetric(name string, delta float64) {
	operational.Lock()
	operational.values[name] += delta
	operational.Unlock()
}

func (a *App) metrics(w http.ResponseWriter, r *http.Request) {
	if !Actor(r).Has("ai-client") {
		Forbidden(w, "A service account is required.")
		return
	}
	operational.Lock()
	values := make(map[string]float64, len(operational.values))
	for key, value := range operational.values {
		values[key] = value
	}
	operational.Unlock()
	stats := a.DB.Stat()
	values["learning_db_pool_connections"] = float64(stats.TotalConns())
	values["learning_db_pool_acquired"] = float64(stats.AcquiredConns())
	values["learning_db_pool_max"] = float64(stats.MaxConns())
	values["learning_db_pool_acquires_total"] = float64(stats.AcquireCount())
	values["learning_db_pool_waits_total"] = float64(stats.EmptyAcquireCount())
	values["learning_db_pool_acquire_seconds_total"] = stats.AcquireDuration().Seconds()
	if a.MetricGauges != nil {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		extra, err := a.MetricGauges(ctx)
		if err != nil {
			Unavailable(w, "Operational measurements unavailable.")
			return
		}
		for key, value := range extra {
			values[key] = value
		}
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if _, err := fmt.Fprintf(w, "%s %g\n", key, values[key]); err != nil {
			return
		}
	}
}
