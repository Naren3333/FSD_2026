package main

import (
	"container/list"
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
