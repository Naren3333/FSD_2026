package main

import (
	"container/list"
	"sync"
)

// vectorCacheSize is how many query vectors are kept.
const vectorCacheSize = 256

type cachedVector struct {
	vector []float32
	model  string
}

// vectorCache keeps the most recently used query vectors, so a repeated question costs no embedding call. When it
// is full the least recently used one makes room. A nil cache keeps nothing.
type vectorCache struct {
	mu      sync.Mutex
	order   *list.List // of *vectorEntry, most recently used first
	entries map[string]*list.Element
}

type vectorEntry struct {
	query string
	value cachedVector
}

func newVectorCache() *vectorCache {
	return &vectorCache{order: list.New(), entries: map[string]*list.Element{}}
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
	c.order.MoveToFront(e)
	return e.Value.(*vectorEntry).value, true
}

func (c *vectorCache) put(query string, v cachedVector) {
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if e, ok := c.entries[query]; ok {
		e.Value.(*vectorEntry).value = v
		c.order.MoveToFront(e)
		return
	}
	if c.order.Len() >= vectorCacheSize {
		oldest := c.order.Back()
		c.order.Remove(oldest)
		delete(c.entries, oldest.Value.(*vectorEntry).query)
	}
	c.entries[query] = c.order.PushFront(&vectorEntry{query, v})
}
