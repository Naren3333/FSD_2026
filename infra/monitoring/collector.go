package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"slices"
	"sort"
	"sync"
	"time"

	"github.com/segmentio/kafka-go"
	"learning/platform"
)

const (
	collectionInterval = 15 * time.Second
	latencyWindow      = 5 * time.Minute
	maxSampleGap       = 25 * time.Second
	probeTimeout       = 3 * time.Second
	topicTimeout       = 12 * time.Second
)

type replicaStatus struct {
	Replica      int    `json:"replica"`
	State        string `json:"state"`
	ReadyProbeMS *int64 `json:"ready_probe_ms"`
}

type latency struct {
	State          string `json:"state"`
	Requests       uint64 `json:"requests"`
	P95MS          *int   `json:"p95_ms"`
	AboveMaxBucket bool   `json:"above_max_bucket"`
	Covered        int    `json:"covered_replicas"`
}

type serviceStatus struct {
	Name        string          `json:"name"`
	State       string          `json:"state"`
	CheckedAt   *time.Time      `json:"checked_at"`
	LastReadyAt *time.Time      `json:"last_ready_at,omitempty"`
	Ready       int             `json:"ready_replicas"`
	Replicas    []replicaStatus `json:"replicas"`
	Latency     latency         `json:"latency"`
}

type topicStatus struct {
	Name          string     `json:"name"`
	State         string     `json:"state"`
	Retained      *int64     `json:"estimated_retained"`
	CheckedAt     *time.Time `json:"checked_at"`
	LastSuccessAt *time.Time `json:"last_success_at"`
}

type statusSnapshot struct {
	Services    []serviceStatus `json:"services"`
	DeadLetters []topicStatus   `json:"dead_letters"`
}

type measurement struct {
	at time.Time
	platform.HTTPHeaderHistogram
}

type collector struct {
	mu          sync.RWMutex
	status      statusSnapshot
	collectedAt time.Time
	client      *http.Client
	token       *platform.ServiceToken
	kafka       *kafka.Client
	history     []map[string][]measurement
	slots       chan struct{}
	lookup      func(context.Context, string, string) ([]net.IP, error)
}

func newCollector(client *http.Client, token *platform.ServiceToken, broker *kafka.Client) *collector {
	c := &collector{
		client: client, token: token, kafka: broker, slots: make(chan struct{}, 24),
		lookup: net.DefaultResolver.LookupIP, history: make([]map[string][]measurement, len(services)),
	}
	for i, name := range services {
		c.status.Services = append(c.status.Services, serviceStatus{
			Name: name, State: "checking", Replicas: []replicaStatus{}, Latency: latency{State: "checking"},
		})
		c.history[i] = make(map[string][]measurement)
	}
	for _, name := range deadLetterTopics {
		c.status.DeadLetters = append(c.status.DeadLetters, topicStatus{Name: name, State: "checking"})
	}
	return c
}

func (c *collector) snapshot() statusSnapshot {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.status
}

func (c *collector) ready() bool {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return time.Since(c.collectedAt) <= 2*collectionInterval
}

func (c *collector) run(ctx context.Context) {
	ticker := time.NewTicker(collectionInterval)
	defer ticker.Stop()
	for ctx.Err() == nil {
		c.collect(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// each lookup and probe has its own timeout, so a replica queued for a probe slot is never reported down.
func (c *collector) collect(ctx context.Context) {
	at := time.Now().UTC()
	previous := c.snapshot()
	next := statusSnapshot{Services: make([]serviceStatus, len(services))}
	var work sync.WaitGroup
	for i := range services {
		work.Go(func() { next.Services[i] = c.checkService(ctx, i, previous.Services[i].LastReadyAt, at) })
	}
	work.Go(func() {
		topics, cancel := context.WithTimeout(ctx, topicTimeout)
		defer cancel()
		next.DeadLetters = c.checkTopics(topics, previous.DeadLetters, at)
	})
	work.Wait()
	c.mu.Lock()
	c.status = next
	c.collectedAt = time.Now()
	c.mu.Unlock()
}

type replicaCheck struct {
	status replicaStatus
	metric platform.HTTPHeaderHistogram
}

func (c *collector) checkService(ctx context.Context, index int, lastReady *time.Time, at time.Time) serviceStatus {
	name := services[index]
	row := serviceStatus{
		Name: name, State: "discovery_failed", CheckedAt: &at, LastReadyAt: lastReady,
		Replicas: []replicaStatus{}, Latency: latency{State: "unavailable"},
	}
	history := c.history[index]
	pruneHistory(history, at.Add(-latencyWindow))
	dns, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	ips, err := c.lookup(dns, "ip", name)
	if err != nil || len(ips) == 0 || len(ips) > 64 {
		return row
	}
	addresses := make([]string, 0, len(ips))
	for _, ip := range ips {
		addresses = append(addresses, ip.String())
	}
	sort.Strings(addresses)
	addresses = slices.Compact(addresses)
	checks := make([]replicaCheck, len(addresses))
	var group sync.WaitGroup
	for i, address := range addresses {
		group.Go(func() { checks[i] = c.checkReplica(ctx, i+1, address) })
	}
	group.Wait()
	return summarizeService(row, checks, addresses, history, at)
}

func summarizeService(row serviceStatus, checks []replicaCheck, addresses []string, history map[string][]measurement, at time.Time) serviceStatus {
	seen := 0
	covered := 0
	anyMetric := false
	for i, check := range checks {
		row.Replicas = append(row.Replicas, check.status)
		if check.status.State != "unreachable" {
			seen++
		}
		if check.status.State == "ready" {
			row.Ready++
		}
		if check.metric.Instance != "" {
			anyMetric = true
			key := addresses[i]
			sample := measurement{at: at, HTTPHeaderHistogram: check.metric}
			prior := history[key]
			if len(prior) > 0 && comparableSamples(prior[len(prior)-1], sample) {
				covered++
			}
			history[key] = append(prior, sample)
		}
	}
	switch {
	case row.Ready == len(checks):
		row.State = "ready"
		row.LastReadyAt = &at
	case row.Ready > 0:
		row.State = "degraded"
	case seen > 0:
		row.State = "not_ready"
	default:
		row.State = "unreachable"
	}
	row.Latency = rollingLatency(history, covered, len(checks), anyMetric)
	return row
}

func pruneHistory(history map[string][]measurement, cutoff time.Time) {
	for key, samples := range history {
		first := 0
		for first < len(samples) && samples[first].at.Before(cutoff) {
			first++
		}
		if first == len(samples) {
			delete(history, key)
		} else {
			history[key] = samples[first:]
		}
	}
}

func comparableSamples(previous, current measurement) bool {
	if previous.Instance != current.Instance || current.at.Sub(previous.at) > maxSampleGap {
		return false
	}
	for i, count := range current.Counts {
		if count < previous.Counts[i] {
			return false
		}
	}
	return true
}

func rollingLatency(history map[string][]measurement, covered, replicas int, anyMetric bool) latency {
	result := latency{State: "checking", Covered: covered}
	if covered == 0 && !anyMetric {
		result.State = "unavailable"
	}
	if covered == 0 {
		return result
	}
	bounds := platform.HTTPHeaderBoundsMS()
	counts := make([]uint64, len(bounds)+1)
	for _, samples := range history {
		for i := 1; i < len(samples); i++ {
			previous, current := samples[i-1], samples[i]
			if !comparableSamples(previous, current) {
				continue
			}
			for b, count := range current.Counts {
				delta := count - previous.Counts[b]
				counts[b] += delta
				result.Requests += delta
			}
		}
	}
	result.State = "available"
	if covered < replicas {
		result.State = "partial"
	}
	if result.Requests == 0 {
		return result
	}
	rank := result.Requests - result.Requests/20
	var cumulative uint64
	for bucket, count := range counts {
		cumulative += count
		if cumulative >= rank {
			if bucket < len(bounds) {
				result.P95MS = &bounds[bucket]
			} else {
				result.AboveMaxBucket = true
			}
			break
		}
	}
	return result
}

func (c *collector) checkReplica(ctx context.Context, replica int, address string) replicaCheck {
	check := replicaCheck{status: replicaStatus{Replica: replica, State: "unreachable"}}
	target := "http://" + net.JoinHostPort(address, "8080")
	select {
	case c.slots <- struct{}{}:
		defer func() { <-c.slots }()
	case <-ctx.Done():
		return check
	}
	var ready, live int
	var elapsed int64
	var group sync.WaitGroup
	group.Go(func() { ready, elapsed = c.probe(ctx, target+"/health/ready") })
	group.Go(func() { live, _ = c.probe(ctx, target+"/health/live") })
	group.Go(func() { check.metric = c.readHistogram(ctx, target) })
	group.Wait()
	if ready != 0 {
		check.status.ReadyProbeMS = &elapsed
	}
	if ready == http.StatusOK && live == http.StatusOK {
		check.status.State = "ready"
	} else if ready != 0 || live != 0 {
		check.status.State = "not_ready"
	}
	return check
}

func (c *collector) probe(ctx context.Context, target string) (int, int64) {
	start := time.Now()
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return 0, 0
	}
	res, err := c.client.Do(req)
	if err != nil {
		return 0, 0
	}
	elapsed := time.Since(start).Milliseconds()
	defer res.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 4096))
	return res.StatusCode, elapsed
}

func (c *collector) readHistogram(ctx context.Context, target string) platform.HTTPHeaderHistogram {
	ctx, cancel := context.WithTimeout(ctx, probeTimeout)
	defer cancel()
	var empty platform.HTTPHeaderHistogram
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target+"/internal/http-metrics", nil)
	if err != nil {
		return empty
	}
	res, err := monitoringGet(c.client, c.token, req)
	if err != nil {
		return empty
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return empty
	}
	var payload platform.HTTPHeaderHistogram
	if json.NewDecoder(io.LimitReader(res.Body, 4096)).Decode(&payload) != nil ||
		!platform.ID(payload.Instance) || !slices.Equal(payload.BoundsMS, platform.HTTPHeaderBoundsMS()) ||
		len(payload.Counts) != len(payload.BoundsMS)+1 {
		return empty
	}
	return payload
}
