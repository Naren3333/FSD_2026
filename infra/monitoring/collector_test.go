package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/segmentio/kafka-go"
	"learning/platform"
)

const histogramInstance = "21354ab9-b030-465d-8132-42d54d5b74d6"

type testHTTPTransport http.HandlerFunc

func (h testHTTPTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	w := httptest.NewRecorder()
	h(w, r)
	return w.Result(), nil
}

func TestHistogramRefreshesRejectedToken(t *testing.T) {
	var fetches atomic.Int32
	var requests atomic.Int32
	client := &http.Client{Transport: testHTTPTransport(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/token" {
			_, _ = fmt.Fprintf(w, `{"access_token":"token-%d","expires_in":300}`, fetches.Add(1))
			return
		}
		requests.Add(1)
		if r.Header.Get("Authorization") != "Bearer token-2" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		histogram := testHistogram(histogramInstance, 2, 3)
		histogram.BoundsMS = platform.HTTPHeaderBoundsMS()
		_ = json.NewEncoder(w).Encode(histogram)
	})}
	token := &platform.ServiceToken{URL: "http://idp.test/token", Client: client}
	c := newCollector(client, token, nil)
	if got := c.readHistogram(t.Context(), "http://service.test"); got.Instance != histogramInstance {
		t.Fatalf("histogram unavailable after %d token and %d histogram requests: %+v", fetches.Load(), requests.Load(), got)
	}
	token.Reject("token-1")
	if got, err := token.Get(t.Context()); err != nil || got != "token-2" {
		t.Fatalf("late rejection invalidated the replacement: %q, %v", got, err)
	}
	if fetches.Load() != 2 || requests.Load() != 2 {
		t.Fatalf("expected two token and histogram requests, got %d and %d", fetches.Load(), requests.Load())
	}
}

func TestHistogramRejectsDifferentBoundsWithSameCount(t *testing.T) {
	client := &http.Client{Transport: testHTTPTransport(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/token" {
			_, _ = w.Write([]byte(`{"access_token":"token","expires_in":300}`))
			return
		}
		histogram := testHistogram(histogramInstance, 2, 3)
		histogram.BoundsMS = platform.HTTPHeaderBoundsMS()
		histogram.BoundsMS[0]++
		_ = json.NewEncoder(w).Encode(histogram)
	})}
	token := &platform.ServiceToken{URL: "http://idp.test/token", Client: client}
	c := newCollector(client, token, nil)
	if got := c.readHistogram(t.Context(), "http://service.test"); got.Instance != "" {
		t.Fatalf("accepted a histogram with a different bucket boundary: %+v", got)
	}
}

func testHistogram(instance string, fast, slow uint64) platform.HTTPHeaderHistogram {
	counts := make([]uint64, len(platform.HTTPHeaderBoundsMS())+1)
	counts[0], counts[1] = fast, slow
	return platform.HTTPHeaderHistogram{Instance: instance, Counts: counts}
}

func TestRollingP95AcrossReplicas(t *testing.T) {
	at := time.Now()
	history := map[string][]measurement{
		"a": {
			{at.Add(-30 * time.Second), testHistogram("a", 900, 900)},
			{at.Add(-15 * time.Second), testHistogram("a", 985, 915)},
		},
		"b": {{at.Add(-15 * time.Second), testHistogram("b", 2000, 50)}},
	}
	row := serviceStatus{}
	checks := []replicaCheck{
		{status: replicaStatus{State: "ready"}, metric: testHistogram("a", 985, 915)},
		{status: replicaStatus{State: "ready"}, metric: testHistogram("b", 2300, 50)},
	}
	got := summarizeService(row, checks, []string{"a", "b"}, history, at)
	if got.State != "ready" || got.Ready != 2 || got.Latency.Requests != 400 || got.Latency.P95MS == nil || *got.Latency.P95MS != 10 {
		t.Fatalf("expected a request-weighted p95 of 10 ms, got %+v", got)
	}
	checks[0] = replicaCheck{status: replicaStatus{State: "unreachable"}}
	checks[1].metric = testHistogram("b", 2304, 50)
	got = summarizeService(row, checks, []string{"a", "b"}, history, at.Add(15*time.Second))
	if got.State != "degraded" || got.Ready != 1 || got.Latency.State != "partial" || got.Latency.Covered != 1 || got.Latency.Requests != 404 {
		t.Fatalf("lost replica should leave recent data with partial coverage, got %+v", got)
	}
}

func TestLatencyRestartsGapsAndZeroTraffic(t *testing.T) {
	at := time.Now()
	row := serviceStatus{}
	addresses := []string{"replica"}
	history := make(map[string][]measurement)
	read := func(offset time.Duration, instance string, count uint64) latency {
		checks := []replicaCheck{{status: replicaStatus{State: "ready"}, metric: testHistogram(instance, count, 0)}}
		return summarizeService(row, checks, addresses, history, at.Add(offset)).Latency
	}
	if got := read(0, "old", 100); got.State != "checking" {
		t.Fatalf("initial sample: %+v", got)
	}
	if got := read(15*time.Second, "new", 200); got.State != "checking" || got.Requests != 0 {
		t.Fatalf("restart counted preexisting traffic: %+v", got)
	}
	if got := read(30*time.Second, "new", 150); got.State != "checking" {
		t.Fatalf("counter reset was accepted: %+v", got)
	}
	if got := read(60*time.Second, "new", 500); got.State != "checking" {
		t.Fatalf("scrape gap was counted: %+v", got)
	}
	if got := read(75*time.Second, "new", 500); got.State != "available" || got.Requests != 0 || got.P95MS != nil {
		t.Fatalf("no traffic: %+v", got)
	}
	pruneHistory(history, at.Add(latencyWindow))
	if len(history) != 0 {
		t.Fatal("expired samples retained")
	}
}

func TestDeadLetterOffsetSpans(t *testing.T) {
	offsets := []kafka.PartitionOffsets{
		{FirstOffset: 80, LastOffset: 80},
		{FirstOffset: 21, LastOffset: 38},
	}
	if got, ok := sumOffsetSpans(offsets, 2); !ok || got != 17 {
		t.Fatalf("invalid retained span %d (%v)", got, ok)
	}
	if _, ok := sumOffsetSpans(offsets, 3); ok {
		t.Fatal("missing partition was ignored")
	}
	offsets[0].Error = kafka.NotLeaderForPartition
	if _, ok := sumOffsetSpans(offsets, 2); ok {
		t.Fatal("partition error was treated as zero")
	}
}
