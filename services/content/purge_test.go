package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"maps"
	"net/http"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/google/uuid"
)

// failingFolders fails every removal.
type failingFolders struct{}

func (failingFolders) removeAll(context.Context, string) error { return errors.New("storage down") }

// purgeWith applies Identity's deletion of the classroom in one transaction, as the consumer does, committing only
// when the purge succeeds.
func (f *fixture) purgeWith(files folders, classroom string) error {
	ctx := context.Background()
	data, _ := json.Marshal(platform.ClassroomDeleted{Classroom: classroom, Org: f.org})
	tx, err := f.db.Begin(ctx)
	if err != nil {
		f.t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err = (purger{files}).purge(ctx, tx, platform.Event{Resource: classroom, Data: data}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// objectsOf counts the stored objects of the document.
func (f *fixture) objectsOf(document string) int {
	n := 0
	for key := range f.store.objects {
		if strings.HasPrefix(key, f.org+"/"+document+"/") {
			n++
		}
	}
	return n
}

func TestAPurgeLeavesOtherClassroomsAlone(t *testing.T) {
	f := newFixture(t)
	deleted := f.upload("teaching_material")
	f.runAll()
	deletedRoom, kept := f.room, uuid.NewString()
	f.room = kept
	other := f.upload("rubric") // its uploaded event is still in the outbox
	f.runAll()
	if f.objectsOf(deleted) != 2 || f.objectsOf(other) != 2 {
		t.Fatalf("objects before the purge: %v", f.store.objects)
	}
	if err := f.purgeWith(f.store, deletedRoom); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT string_agg(id::text,',') FROM documents"); got != other {
		t.Fatalf("documents left: %s", got)
	}
	if got := f.value("SELECT count(DISTINCT document_id) FROM document_chunks"); got != "1" {
		t.Fatalf("documents with chunks: %s", got)
	}
	if f.objectsOf(deleted) != 0 || f.objectsOf(other) != 2 {
		t.Fatalf("objects after the purge: %v", f.store.objects)
	}
	if got := f.value("SELECT count(*) FROM outbox WHERE body->'data'->>'classroom_id'=$1", kept); got == "0" {
		t.Fatal("the other classroom's events were discarded")
	}
	if got := f.value("SELECT count(*) FROM outbox WHERE body->'data'->>'classroom_id'=$1", deletedRoom); got != "0" {
		t.Fatalf("%s events of the deleted classroom remain", got)
	}
	if got := f.value("SELECT string_agg(classroom_id::text,',') FROM purged_classrooms"); got != deletedRoom {
		t.Fatalf("purged: %s", got)
	}
	// A repeated event changes nothing.
	if err := f.purgeWith(f.store, deletedRoom); err != nil || f.objectsOf(other) != 2 {
		t.Fatalf("repeat: %v", err)
	}
}

func TestAPurgeWhoseObjectsCannotBeRemovedKeepsTheRows(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.runAll()
	if err := f.purgeWith(failingFolders{}, f.room); err == nil {
		t.Fatal("the purge succeeded without removing the objects")
	}
	if got := f.value("SELECT (SELECT count(*) FROM documents)||' '||(SELECT count(*) FROM document_chunks)||' '||(SELECT count(*) FROM purged_classrooms)"); got != "1 2 0" {
		t.Fatalf("documents, chunks, purged: %s", got)
	}
	// The retry removes everything.
	if err := f.purgeWith(f.store, f.room); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT count(*) FROM documents"); got != "0" || f.objectsOf(id) != 0 {
		t.Fatalf("after the retry: %s documents, %d objects", got, f.objectsOf(id))
	}
}

func TestDeletingADocumentRemovesItsChunksJobObjectsAndEmitsOnce(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	deleted, kept := f.upload("teaching_material"), f.upload("teaching_material")
	f.runAll()
	if err := f.s.store.deleteDocument(ctx, f.org, deleted, func(ctx context.Context) error {
		return f.store.removeAll(ctx, f.org+"/"+deleted+"/")
	}); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT string_agg(id::text,',') FROM documents"); got != kept {
		t.Fatalf("documents left: %s", got)
	}
	if got := f.value("SELECT count(*) FROM document_chunks WHERE document_id=$1", deleted); got != "0" {
		t.Fatalf("%s chunks left", got)
	}
	if f.objectsOf(deleted) != 0 || f.objectsOf(kept) != 2 {
		t.Fatalf("objects: %v", f.store.objects)
	}
	if got := f.value("SELECT count(*) FROM outbox WHERE topic='content.deleted.v1' AND body->'data'->>'document_id'=$1 AND body->'data'->>'classroom_id'=$2", deleted, f.room); got != "1" {
		t.Fatalf("%s deleted events", got)
	}
	contentDeletedMatchesItsContract(t, f.value("SELECT body::text FROM outbox WHERE topic='content.deleted.v1'"), deleted)
	// Deleting it again finds nothing and emits nothing.
	err := f.s.store.deleteDocument(ctx, f.org, deleted, func(context.Context) error { return nil })
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("second delete: %v", err)
	}
	// Another organization's id is not found either.
	if err = f.s.store.deleteDocument(ctx, "org-2", kept, func(context.Context) error { return nil }); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("other organization: %v", err)
	}
}

func TestADeleteWhoseObjectsCannotBeRemovedKeepsTheRow(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.runAll()
	if err := f.s.store.deleteDocument(context.Background(), f.org, id, func(ctx context.Context) error {
		return failingFolders{}.removeAll(ctx, "")
	}); err == nil {
		t.Fatal("the delete succeeded without removing the objects")
	}
	if got := f.value("SELECT (SELECT count(*) FROM documents)||' '||(SELECT count(*) FROM outbox WHERE topic='content.deleted.v1')"); got != "1 0" {
		t.Fatalf("documents, deleted events: %s", got)
	}
}

func TestADeleteDuringIndexingLeavesNoCrops(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	ctx := context.Background()
	running, err := f.x.claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// The teacher deletes the document while the run is with ai-core; the run then stores its crops and loses its
	// lease.
	reply := f.reply(indexedResult())
	f.ai.ingest = func(w http.ResponseWriter, r *http.Request) {
		if err := f.s.store.deleteDocument(ctx, f.org, id, func(ctx context.Context) error {
			return f.store.removeAll(ctx, f.org+"/"+id+"/")
		}); err != nil {
			t.Error(err)
		}
		reply(w, r)
	}
	if err = f.x.process(ctx, running); err != nil {
		t.Fatal(err)
	}
	if f.objectsOf(id) != 0 {
		t.Fatalf("objects survived the delete: %v", f.store.objects)
	}
	if got := f.value("SELECT count(*) FROM index_jobs"); got != "0" {
		t.Fatalf("%s jobs left", got)
	}
}

// contentDeletedMatchesItsContract checks the event body has exactly the fields contracts/events/content.deleted.v1
// requires, at the top level and in its data, and names the deleted document.
func contentDeletedMatchesItsContract(t *testing.T, body, document string) {
	t.Helper()
	raw, err := os.ReadFile("../../contracts/events/content.deleted.v1.schema.json")
	if err != nil {
		t.Fatal(err)
	}
	type object struct {
		Required   []string                   `json:"required"`
		Properties map[string]json.RawMessage `json:"properties"`
	}
	var schema object
	var data object
	var event map[string]any
	if err = json.Unmarshal(raw, &schema); err == nil {
		err = json.Unmarshal(schema.Properties["data"], &data)
	}
	if err == nil {
		err = json.Unmarshal([]byte(body), &event)
	}
	if err != nil {
		t.Fatal(err)
	}
	fields, _ := event["data"].(map[string]any)
	if !slices.Equal(slices.Sorted(maps.Keys(event)), slices.Sorted(slices.Values(schema.Required))) ||
		!slices.Equal(slices.Sorted(maps.Keys(fields)), slices.Sorted(slices.Values(data.Required))) {
		t.Fatalf("the event does not have the contract's fields: %s", body)
	}
	if event["event_type"] != "content.deleted.v1" || event["resource_id"] != document || fields["document_id"] != document {
		t.Fatalf("the event names the wrong type or document: %s", body)
	}
}
