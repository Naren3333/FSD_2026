package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
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
