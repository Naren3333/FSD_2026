package platform

import (
	"net/http"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
)

var httpHeaderBoundsMS = [...]int{10, 50, 100, 500, 1000, 5000, 30000, 60000, 120000, 300000}

// the upper edges of the finite, disjoint histogram buckets, in milliseconds.
func HTTPHeaderBoundsMS() []int {
	return append([]int(nil), httpHeaderBoundsMS[:]...)
}

// the disjoint bucket counts for one process lifetime.
type HTTPHeaderHistogram struct {
	Instance string   `json:"instance"`
	BoundsMS []int    `json:"bounds_ms"`
	Counts   []uint64 `json:"bucket_counts"`
}

var headerMetricsInstance = uuid.NewString()
var headerMetricCounts [len(httpHeaderBoundsMS) + 1]atomic.Uint64

func observeHTTPHeaders(elapsed time.Duration) {
	bucket := len(httpHeaderBoundsMS)
	for i, bound := range httpHeaderBoundsMS {
		if elapsed <= time.Duration(bound)*time.Millisecond {
			bucket = i
			break
		}
	}
	headerMetricCounts[bucket].Add(1)
}

func httpHeaderMetrics(w http.ResponseWriter, r *http.Request) {
	if !Actor(r).Has(RoleMonitoringReader) {
		Forbidden(w, "A monitoring service account is required.")
		return
	}
	counts := make([]uint64, len(headerMetricCounts))
	for i := range counts {
		counts[i] = headerMetricCounts[i].Load()
	}
	w.Header().Set("Cache-Control", "no-store")
	JSON(w, http.StatusOK, HTTPHeaderHistogram{
		Instance: headerMetricsInstance,
		BoundsMS: HTTPHeaderBoundsMS(),
		Counts:   counts,
	})
}
