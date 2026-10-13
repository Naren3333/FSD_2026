package main

import (
	"strconv"
	"testing"
)

func TestTheLeastRecentlyUsedVectorMakesRoom(t *testing.T) {
	c := newVectorCache()
	for i := range vectorCacheSize {
		c.put(strconv.Itoa(i), cachedVector{model: strconv.Itoa(i)})
	}
	c.get("0") // used again, so "1" is now the oldest
	c.put("new", cachedVector{model: "new"})
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
