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

// failingfolders fails every removal.
type failingFolders struct{}

func (failingFolders) removeAll(context.Context, string) error { return errors.New("storage down") }

// purgewith applies identity's deletion of the classroom in one transaction, as the consumer does,
// committing only when the purge succeeds.
func (f *fixture) purgeWith(classroom string) error {
	ctx := context.Background()
	data, _ := json.Marshal(platform.ClassroomDeleted{Classroom: classroom, Org: f.org})
	tx, err := f.db.Begin(ctx)
	if err != nil {
		f.t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err = (purger{}).purge(ctx, tx, platform.Event{Resource: classroom, Data: data}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// objectsof counts the stored objects of the document.
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
	if err := f.purgeWith(deletedRoom); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT string_agg(id::text,',') FROM documents"); got != other {
		t.Fatalf("documents left: %s", got)
	}
	if got := f.value("SELECT count(DISTINCT document_id) FROM document_chunks"); got != "1" {
		t.Fatalf("documents with chunks: %s", got)
	}
	f.clean()
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
	// a repeated event changes nothing.
	if err := f.purgeWith(deletedRoom); err != nil || f.objectsOf(other) != 2 {
		t.Fatalf("repeat: %v", err)
	}
}

// cleanup failures do not restore access; the job is durable and can be retried.
func TestAPurgeQueuesCleanupAndRetriesStorageFailure(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.runAll()
	if err := f.purgeWith(f.room); err != nil {
		t.Fatal(err)
	}
	if f.value("SELECT count(*) FROM documents") != "0" || f.objectsOf(id) != 2 {
		t.Fatal("purge did not commit before object removal")
	}
	c := cleaner{f.db, failingFolders{}}
	if found, err := c.next(t.Context()); !found || err == nil {
		t.Fatal("expected cleanup failure")
	}
	if f.value("SELECT count(*) FROM object_cleanup_jobs") != "1" {
		t.Fatal("lost cleanup job")
	}
	f.exec("UPDATE object_cleanup_jobs SET next_attempt=now()")
	f.clean()
	if f.objectsOf(id) != 0 {
		t.Fatal("retry left objects")
	}
}

func (f *fixture) clean() {
	f.t.Helper()
	for {
		found, err := (cleaner{f.db, f.store}).next(context.Background())
		if err != nil {
			f.t.Fatal(err)
		}
		if !found {
			return
		}
	}
}

func TestDeletingADocumentRemovesItsChunksJobObjectsAndEmitsOnce(t *testing.T) {
	f := newFixture(t)
	ctx := context.Background()
	deleted, kept := f.upload("teaching_material"), f.upload("teaching_material")
	f.runAll()
	if err := f.s.store.deleteDocument(ctx, f.org, deleted); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT string_agg(id::text,',') FROM documents"); got != kept {
		t.Fatalf("documents left: %s", got)
	}
	if got := f.value("SELECT count(*) FROM document_chunks WHERE document_id=$1", deleted); got != "0" {
		t.Fatalf("%s chunks left", got)
	}
	f.clean()
	if f.objectsOf(deleted) != 0 || f.objectsOf(kept) != 2 {
		t.Fatalf("objects: %v", f.store.objects)
	}
	if got := f.value("SELECT count(*) FROM outbox WHERE topic='content.deleted.v1' AND body->'data'->>'document_id'=$1 AND body->'data'->>'classroom_id'=$2", deleted, f.room); got != "1" {
		t.Fatalf("%s deleted events", got)
	}
	contentDeletedMatchesItsContract(t, f.value("SELECT body::text FROM outbox WHERE topic='content.deleted.v1'"), deleted)
	// deleting it again finds nothing and emits nothing.
	err := f.s.store.deleteDocument(ctx, f.org, deleted)
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("second delete: %v", err)
	}
	// another organization's id is not found either.
	if err = f.s.store.deleteDocument(ctx, "org-2", kept); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("other organization: %v", err)
	}
}

func TestADeleteRollbackCannotRemoveObjectsOrQueueCleanup(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.runAll()
	// a failure in the deletion transaction must leave both data and objects intact.
	f.exec("ALTER TABLE object_cleanup_jobs ADD CONSTRAINT reject_cleanup CHECK(false)")
	if err := f.s.store.deleteDocument(t.Context(), f.org, id); err == nil {
		t.Fatal("expected rollback")
	}
	if f.value("SELECT count(*) FROM documents") != "1" || f.value("SELECT count(*) FROM outbox WHERE topic='content.deleted.v1'") != "0" || f.objectsOf(id) != 2 {
		t.Fatal("rollback lost data")
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
	// the teacher deletes the document while the run is with ai-core; the run then stores its crops and loses
	// its lease.
	reply := f.reply(indexedResult())
	f.ai.ingest = func(w http.ResponseWriter, r *http.Request) {
		if err := f.s.store.deleteDocument(ctx, f.org, id); err != nil {
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

// contentdeletedmatchesitscontract checks the event body has exactly the fields
// contracts/events/content.deleted.v1 requires, at the top level and in its data, and names the deleted
// document.
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

func (f *fixture) exec(sql string) {
	f.t.Helper()
	if _, err := f.db.Exec(context.Background(), sql); err != nil {
		f.t.Fatal(err)
	}
}
