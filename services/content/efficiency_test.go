package main

import (
	"context"
	"learning/platform"
	"sync"
	"sync/atomic"
	"testing"
)

func TestConcurrentQueryMissesShareOnePaidEmbedding(t *testing.T) {
	cache := newVectorCache()
	var calls atomic.Int32
	started, finish := make(chan struct{}), make(chan struct{})
	fetch := func(context.Context) (cachedVector, error) {
		if calls.Add(1) == 1 {
			close(started)
		}
		<-finish
		return cachedVector{model: "m"}, nil
	}
	var wg sync.WaitGroup
	for range 20 {
		wg.Go(func() {
			v, err := cache.load(t.Context(), "same query", fetch)
			if err != nil || v.model != "m" {
				t.Errorf("%+v %v", v, err)
			}
		})
	}
	<-started
	close(finish)
	wg.Wait()
	if calls.Load() != 1 {
		t.Fatalf("made %d paid calls", calls.Load())
	}
}

func TestDocumentPagesKeepTheirAccessFilterAndReachOlderRows(t *testing.T) {
	f := newFixture(t)
	for range 25 {
		f.upload("teaching_material")
	}
	for range 3 {
		f.upload("rubric")
	}
	p := pgStore{f.db}
	rows, err := p.listDocuments(t.Context(), f.org, f.room, false, platform.ListWindow{Offset: 20, Limit: 20})
	if err != nil || len(rows) != 5 {
		t.Fatalf("student page: %d %v", len(rows), err)
	}
	for _, row := range rows {
		if row.DocType != "teaching_material" {
			t.Fatal("student received teacher-only material")
		}
	}
	total, err := p.countDocuments(t.Context(), f.org, f.room, false)
	if err != nil || total != 25 {
		t.Fatalf("student total: %d %v", total, err)
	}
	total, err = p.countDocuments(t.Context(), f.org, f.room, true)
	if err != nil || total != 28 {
		t.Fatalf("teacher total: %d %v", total, err)
	}
}
