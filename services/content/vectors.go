package main

import "sync"

// vectorCacheSize is how many query vectors are kept.
const vectorCacheSize = 256

type cachedVector struct {
	vector []float32
	model  string
}

// vectorCache keeps recent query vectors, so a repeated question costs no embedding call. A nil cache keeps
// nothing.
type vectorCache struct {
	mu      sync.Mutex
	entries map[string]cachedVector
}

func newVectorCache() *vectorCache { return &vectorCache{entries: map[string]cachedVector{}} }

func (c *vectorCache) get(query string) (cachedVector, bool) {
	if c == nil {
		return cachedVector{}, false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	v, ok := c.entries[query]
	return v, ok
}

func (c *vectorCache) put(query string, v cachedVector) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if len(c.entries) >= vectorCacheSize {
		clear(c.entries)
	}
	c.entries[query] = v
}
