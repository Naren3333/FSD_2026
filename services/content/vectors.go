package main

import (
	"container/list"
	"context"
	"learning/platform"
	"log/slog"
	"sync"
	"time"
)

const (
	// vectorCacheSize is how many query vectors are kept.
	vectorCacheSize = 256
	// vectorCacheTTL is how long a query vector is used before it is embedded again. It bounds how long search keeps
	// using an embedding model that ai-core has since replaced.
	vectorCacheTTL = 10 * time.Minute
)

type cachedVector struct {
	vector []float32
	model  string
}

// vectorCache keeps the most recently used query vectors, so a repeated question costs no embedding call. When it
// is full the least recently used one makes room. Vectors are kept for one embedding model, the one ai-core used
// last: a vector from another model makes the cache start again, so a query is never compared using a model that
// the indexed chunks no longer use. A nil cache keeps nothing.
type vectorCache struct {
	mu      sync.Mutex
	model   string     // the embedding model of every vector kept
	order   *list.List // of *vectorEntry, most recently used first
	entries map[string]*list.Element
	now     func() time.Time
	flights map[string]*vectorFlight
}

type vectorFlight struct {
	done  chan struct{}
	value cachedVector
	err   error
}

// load shares an in-flight embedding, never passages or authorization decisions. The initiating caller pays;
// a cancelled waiter stops waiting without cancelling work another authorized caller is using.
func (c *vectorCache) load(ctx context.Context, query string, fetch func(context.Context) (cachedVector, error)) (cachedVector, error) {
	if c == nil {
		return fetch(ctx)
	}
	if v, ok := c.get(query); ok {
		platform.CountMetric("learning_query_vector_cache_hits_total", 1)
		slog.Info("query_vector_cache", "hit", true)
		return v, nil
	}
	c.mu.Lock()
	if c.flights == nil {
		c.flights = map[string]*vectorFlight{}
	}
	f, exists := c.flights[query]
	platform.CountMetric("learning_query_vector_cache_misses_total", 1)
	if exists {
		platform.CountMetric("learning_query_vector_coalesced_total", 1)
	}
	if !exists {
		f = &vectorFlight{done: make(chan struct{})}
		c.flights[query] = f
		go func() {
			work, cancel := context.WithTimeout(context.WithoutCancel(ctx), embedTimeout)
			defer cancel()
			if v, ok := c.get(query); ok {
				f.value = v
			} else {
				f.value, f.err = fetch(work)
				if f.err == nil {
					c.put(query, f.value)
				}
			}
			c.mu.Lock()
			delete(c.flights, query)
			close(f.done)
			c.mu.Unlock()
		}()
	}
	c.mu.Unlock()
	slog.Info("query_vector_cache", "hit", false, "coalesced", exists)
	select {
	case <-ctx.Done():
		return cachedVector{}, ctx.Err()
	case <-f.done:
		return f.value, f.err
	}
}

type vectorEntry struct {
	query  string
	value  cachedVector
	stored time.Time
}

func newVectorCache() *vectorCache {
	return &vectorCache{order: list.New(), entries: map[string]*list.Element{}, now: time.Now}
}

func (c *vectorCache) get(query string) (cachedVector, bool) {
	if c == nil {
		return cachedVector{}, false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.entries[query]
	if !ok {
		return cachedVector{}, false
	}
	entry := e.Value.(*vectorEntry)
	if c.now().Sub(entry.stored) >= vectorCacheTTL {
		c.order.Remove(e)
		delete(c.entries, query)
		return cachedVector{}, false
	}
	c.order.MoveToFront(e)
	return entry.value, true
}

func (c *vectorCache) put(query string, v cachedVector) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if v.model != c.model {
		c.model = v.model
		c.order.Init()
		clear(c.entries)
	}
	if e, ok := c.entries[query]; ok {
		e.Value = &vectorEntry{query, v, c.now()}
		c.order.MoveToFront(e)
		return
	}
	if c.order.Len() >= vectorCacheSize {
		oldest := c.order.Back()
		c.order.Remove(oldest)
		delete(c.entries, oldest.Value.(*vectorEntry).query)
	}
	c.entries[query] = c.order.PushFront(&vectorEntry{query, v, c.now()})
}
