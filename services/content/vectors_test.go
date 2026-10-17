package main

import (
	"strconv"
	"testing"
	"time"
)

func TestTheLeastRecentlyUsedVectorMakesRoom(t *testing.T) {
	c := newVectorCache()
	for i := range vectorCacheSize {
		c.put(strconv.Itoa(i), cachedVector{model: "m1"})
	}
	c.get("0") // used again, so "1" is now the oldest
	c.put("new", cachedVector{model: "m1"})
	if _, ok := c.get("1"); ok {
		t.Fatal("the least recently used vector was kept")
	}
	for _, query := range []string{"0", "2", "new"} {
		if _, ok := c.get(query); !ok {
			t.Fatalf("%s was evicted", query)
		}
	}
	var none *vectorCache
	none.put("q", cachedVector{})
	if _, ok := none.get("q"); ok {
		t.Fatal("a nil cache kept a vector")
	}
}

func TestVectorsExpireAndAreKeptForOneModel(t *testing.T) {
	c := newVectorCache()
	clock := time.Now()
	c.now = func() time.Time { return clock }
	c.put("photosynthesis", cachedVector{model: "m1"})
	c.put("osmosis", cachedVector{model: "m1"})
	clock = clock.Add(vectorCacheTTL)
	if _, ok := c.get("photosynthesis"); ok {
		t.Fatal("an expired vector was used")
	}
	// ai-core now embeds with another model: vectors from the old one are no longer used.
	c.put("mitosis", cachedVector{model: "m2"})
	clock = clock.Add(-vectorCacheTTL) // osmosis would otherwise still be fresh
	if _, ok := c.get("osmosis"); ok {
		t.Fatal("a vector from the replaced model was used")
	}
	if v, ok := c.get("mitosis"); !ok || v.model != "m2" {
		t.Fatalf("%+v %v", v, ok)
	}
}
