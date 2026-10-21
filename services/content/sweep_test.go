package main

import (
	"context"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"
)

// agedbucket is the fixture's objects, each written at the time given for it, or long ago.
type agedBucket struct {
	store   *memoryStore
	written map[string]time.Time
}

func (b agedBucket) list(context.Context) ([]storedObject, error) {
	b.store.mu.Lock()
	defer b.store.mu.Unlock()
	objects := []storedObject{}
	for key := range b.store.objects {
		at, ok := b.written[key]
		if !ok {
			at = time.Now().Add(-24 * time.Hour)
		}
		objects = append(objects, storedObject{key, at})
	}
	return objects, nil
}

func (b agedBucket) modified(_ context.Context, key string) (time.Time, bool, error) {
	b.store.mu.Lock()
	defer b.store.mu.Unlock()
	if _, ok := b.store.objects[key]; !ok {
		return time.Time{}, false, nil
	}
	if at, ok := b.written[key]; ok {
		return at, true, nil
	}
	return time.Now().Add(-24 * time.Hour), true, nil
}

func (b agedBucket) remove(_ context.Context, key string) error {
	b.store.mu.Lock()
	defer b.store.mu.Unlock()
	delete(b.store.objects, key)
	return nil
}

func (f *fixture) keys() []string {
	keys := []string{}
	for key := range f.store.objects {
		keys = append(keys, key)
	}
	slices.Sort(keys)
	return keys
}

// the sweeper removes, once they are old enough, the objects no row names: an orphaned original, the crops
// a reindex no longer cites, anything outside a document's prefixes.
func TestTheSweeperRemovesOnlyObjectsNoRowNames(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	indexed := f.upload("teaching_material")
	f.runAll()
	kept := f.keys() // the original and the crop its chunks cite
	indexing := f.upload("teaching_material")
	orphan, recent := uuid.NewString(), uuid.NewString()
	for _, key := range []string{
		f.org + "/" + indexed + "/figures/aaaaaaaaaaaaaaaa.png", // a crop the reindex no longer cites
		f.org + "/" + indexing + "/figures/fedcba9876543210.png",
		f.org + "/" + orphan + "/1", // an upload whose row never committed
		f.org + "/" + recent + "/1", // an upload still committing
		"stray-key",
	} {
		f.store.put(ctx, key, []byte("x"), mimePNG)
	}
	b := agedBucket{f.store, map[string]time.Time{f.org + "/" + recent + "/1": time.Now()}}
	removed, err := sweeper{f.db, b}.sweep(ctx, time.Now().Add(-sweepGrace))
	if err != nil || removed != 2 {
		t.Fatalf("removed %d, %v; left %v", removed, err, f.keys())
	}
	want := append(slices.Clone(kept), f.org+"/"+indexing+"/1", f.org+"/"+indexing+"/figures/fedcba9876543210.png", f.org+"/"+recent+"/1", "stray-key")
	slices.Sort(want)
	if got := f.keys(); !slices.Equal(got, want) {
		t.Fatalf("left %v, want %v", got, want)
	}
	if again, err := (sweeper{f.db, b}).sweep(ctx, time.Now().Add(-sweepGrace)); err != nil || again != 0 {
		t.Fatalf("a second sweep removed %d, %v", again, err)
	}
}

// restoredbucket is an agedbucket where a reindex stores the objects in restored again after the sweeper
// listed them.
type restoredBucket struct {
	agedBucket
	restored []string
}

func (b restoredBucket) list(ctx context.Context) ([]storedObject, error) {
	objects, err := b.agedBucket.list(ctx)
	for _, key := range b.restored {
		b.written[key] = time.Now()
	}
	return objects, err
}

// a crop stored again between the sweeper's listing and its removal is kept: the reindex that stored it may
// cite it.
func TestTheSweeperKeepsACropStoredAgainMeanwhile(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	indexed := f.upload("teaching_material")
	f.runAll()
	crop, gone := f.org+"/"+indexed+"/figures/aaaaaaaaaaaaaaaa.png", f.org+"/"+indexed+"/figures/bbbbbbbbbbbbbbbb.png"
	for _, key := range []string{crop, gone} {
		f.store.put(ctx, key, []byte("x"), mimePNG)
	}
	b := restoredBucket{agedBucket{f.store, map[string]time.Time{}}, []string{crop}}
	removed, err := sweeper{f.db, b}.sweep(ctx, time.Now().Add(-sweepGrace))
	if err != nil || removed != 1 || !slices.Contains(f.keys(), crop) || slices.Contains(f.keys(), gone) {
		t.Fatalf("removed %d, %v; left %v", removed, err, f.keys())
	}
}

// a reindex queued after the sweep read the rows, before it removes anything, keeps every object of the
// document: the rows are read again under the document's lock.
func TestTheSweeperJudgesAgainUnderTheDocumentsLock(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	indexed := f.upload("teaching_material")
	f.runAll()
	crop := f.org + "/" + indexed + "/figures/aaaaaaaaaaaaaaaa.png"
	f.store.put(ctx, crop, []byte("x"), mimePNG)
	s := sweeper{f.db, agedBucket{f.store, map[string]time.Time{}}}
	stale, err := readOwners(ctx, f.db, []string{indexed})
	if err != nil {
		t.Fatal(err)
	}
	if queued, err := f.s.store.queueIndex(ctx, f.org, indexed); err != nil || !queued {
		t.Fatalf("reindex: %v %v", queued, err)
	}
	removed, err := s.removeUnowned(ctx, map[string][]string{indexed: {crop}}, stale, time.Now().Add(-sweepGrace))
	if err != nil || removed != 0 || !slices.Contains(f.keys(), crop) {
		t.Fatalf("removed %d, %v; left %v", removed, err, f.keys())
	}
}

// lockcheckingbucket is an agedbucket that, as each object is removed, tries to queue a reindex of its
// document and records whether that had to wait.
type lockCheckingBucket struct {
	agedBucket
	queue   func(ctx context.Context) error
	blocked *bool
}

func (b lockCheckingBucket) remove(ctx context.Context, key string) error {
	try, cancel := context.WithTimeout(ctx, 200*time.Millisecond)
	defer cancel()
	*b.blocked = errors.Is(b.queue(try), context.DeadlineExceeded)
	return b.agedBucket.remove(ctx, key)
}

// a reindex cannot be queued while the sweeper removes a document's objects, so no run can store a crop
// again until the removal is done; it is queued once the sweep has finished.
func TestAReindexWaitsForTheSweepersRemovals(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	indexed := f.upload("teaching_material")
	f.runAll()
	crop := f.org + "/" + indexed + "/figures/aaaaaaaaaaaaaaaa.png"
	f.store.put(ctx, crop, []byte("x"), mimePNG)
	blocked := false
	queue := func(ctx context.Context) error {
		_, err := f.s.store.queueIndex(ctx, f.org, indexed)
		return err
	}
	b := lockCheckingBucket{agedBucket{f.store, map[string]time.Time{}}, queue, &blocked}
	removed, err := sweeper{f.db, b}.sweep(ctx, time.Now().Add(-sweepGrace))
	if err != nil || removed != 1 || slices.Contains(f.keys(), crop) || !blocked {
		t.Fatalf("removed %d, %v, reindex waited %v; left %v", removed, err, blocked, f.keys())
	}
	if queued, err := f.s.store.queueIndex(ctx, f.org, indexed); err != nil || !queued {
		t.Fatalf("reindex after the sweep: %v %v", queued, err)
	}
}
