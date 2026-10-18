package main

import (
	"context"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"testing"

	"github.com/google/uuid"
)

// These tests run the store, and the handlers over it, against real PostgreSQL in a throwaway schema with every
// migration applied. They need CURRICULUM_TEST_DATABASE_URL and are skipped without it.

func testStore(t *testing.T) pgStore {
	t.Helper()
	return pgStore{platformtest.DB(t, "CURRICULUM_TEST_DATABASE_URL", migrations)}
}

// topic inserts an active topic and returns its id.
func topic(t *testing.T, s pgStore, org string) string {
	t.Helper()
	id := uuid.NewString()
	if err := s.insertTopic(context.Background(), id, org, "topic "+id[:4]); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestTheCycleCheckFollowsTheWholeChain(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	a, b, c := topic(t, s, "org"), topic(t, s, "org"), topic(t, s, "org")
	for _, edge := range [][2]string{{a, b}, {b, c}, {a, b}} { // A needs B, B needs C; repeating an edge is harmless
		if got, err := s.insertPrerequisite(ctx, "org", edge[0], edge[1]); err != nil || got != prerequisiteAdded {
			t.Fatalf("%v: %v %v", edge, got, err)
		}
	}
	for _, edge := range [][2]string{{c, a}, {c, b}, {b, a}} {
		if got, err := s.insertPrerequisite(ctx, "org", edge[0], edge[1]); err != nil || got != prerequisiteCycle {
			t.Errorf("%v: %v %v", edge, got, err)
		}
	}
	var edges int
	if err := s.db.QueryRow(ctx, "SELECT count(*) FROM prerequisites").Scan(&edges); err != nil || edges != 2 {
		t.Fatalf("%d edges, %v", edges, err)
	}
}

func TestPrerequisitesStayInTheOrganizationAndBetweenActiveTopics(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	mine, theirs := topic(t, s, "org"), topic(t, s, "elsewhere")
	retired := topic(t, s, "org")
	if ok, err := s.archiveTopic(ctx, "org", retired); err != nil || !ok {
		t.Fatal(ok, err)
	}
	for _, edge := range [][2]string{{mine, theirs}, {theirs, mine}, {mine, retired}} {
		if got, err := s.insertPrerequisite(ctx, "org", edge[0], edge[1]); err != nil || got != prerequisiteInvalid {
			t.Errorf("%v: %v %v", edge, got, err)
		}
	}
	// Through the handler, as the other organization's teacher would call it.
	server := server{store: s}.routes
	w := platformtest.Serve(server, http.MethodPost, "/v1/topics/"+mine+"/prerequisites", `{"prerequisite_id":"`+theirs+`"}`, "teacher-1", "teacher")
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("cross-organization prerequisite: %d %s", w.Code, w.Body.String())
	}
}

func TestOtherOrganizationsTopicsCannotBeReadRenamedArchivedOrRestored(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	id := topic(t, s, "org")
	if _, err := s.findTopic(ctx, "elsewhere", id); err == nil {
		t.Error("found another organization's topic")
	}
	if ok, err := s.renameTopic(ctx, "elsewhere", id, "Taken"); err != nil || ok {
		t.Errorf("renamed another organization's topic: %v %v", ok, err)
	}
	if ok, err := s.archiveTopic(ctx, "elsewhere", id); err != nil || ok {
		t.Errorf("archived another organization's topic: %v %v", ok, err)
	}
	if ok, err := s.renameTopic(ctx, "org", id, "Halves"); err != nil || !ok {
		t.Fatalf("rename: %v %v", ok, err)
	}
	if v, err := s.findTopic(ctx, "org", id); err != nil || v.Name != "Halves" {
		t.Fatalf("topic = %v, %v", v, err)
	}
	if list, err := s.listTopics(ctx, "org", false); err != nil || len(list) != 1 || list[0].ID != id {
		t.Fatalf("list = %v, %v", list, err)
	}
	if ok, err := s.archiveTopic(ctx, "org", id); err != nil || !ok {
		t.Fatalf("archive: %v %v", ok, err)
	}
	if _, err := s.findTopic(ctx, "org", id); err == nil {
		t.Fatal("an archived topic is still found")
	}
	if list, err := s.listTopics(ctx, "org", false); err != nil || len(list) != 0 {
		t.Fatalf("active list after archiving = %v, %v", list, err)
	}
	if list, err := s.listTopics(ctx, "elsewhere", true); err != nil || len(list) != 0 {
		t.Fatalf("another organization's archived list = %v, %v", list, err)
	}
	if list, err := s.listTopics(ctx, "org", true); err != nil || len(list) != 1 || list[0] != (Topic{id, "Halves"}) {
		t.Fatalf("archived list = %v, %v", list, err)
	}
	if _, err := s.restoreTopic(ctx, "elsewhere", id); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("restored another organization's topic: %v", err)
	}
	if v, err := s.restoreTopic(ctx, "org", id); err != nil || v != (Topic{id, "Halves"}) {
		t.Fatalf("restore = %v, %v", v, err)
	}
	if _, err := s.findTopic(ctx, "org", id); err != nil {
		t.Fatalf("a restored topic is not found: %v", err)
	}
	if _, err := s.restoreTopic(ctx, "org", id); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("restored an active topic: %v", err)
	}
}

// A topic's prerequisites list only active topics of its organization; a link to an archived one is kept and comes
// back with it, and either can be removed.
func TestPrerequisitesCanBeReadAndRemoved(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	a, b, c := topic(t, s, "org"), topic(t, s, "org"), topic(t, s, "org")
	for _, edge := range [][2]string{{a, b}, {a, c}} {
		if got, err := s.insertPrerequisite(ctx, "org", edge[0], edge[1]); err != nil || got != prerequisiteAdded {
			t.Fatal(got, err)
		}
	}
	if ok, err := s.archiveTopic(ctx, "org", c); err != nil || !ok {
		t.Fatal(ok, err)
	}
	if got, err := s.listPrerequisites(ctx, "org", a); err != nil || len(got) != 1 || got[0].ID != b {
		t.Fatalf("%+v %v", got, err)
	}
	if got, err := s.listPrerequisites(ctx, "elsewhere", a); err != nil || len(got) != 0 {
		t.Fatalf("another organization read %+v %v", got, err)
	}
	if _, err := s.restoreTopic(ctx, "org", c); err != nil {
		t.Fatal(err)
	}
	if got, err := s.listPrerequisites(ctx, "org", a); err != nil || len(got) != 2 {
		t.Fatalf("after restoring: %+v %v", got, err)
	}
	if removed, err := s.deletePrerequisite(ctx, "elsewhere", a, b); err != nil || removed {
		t.Fatalf("another organization removed one: %v %v", removed, err)
	}
	for want := range 2 { // removed once, then gone
		if removed, err := s.deletePrerequisite(ctx, "org", a, b); err != nil || removed != (want == 0) {
			t.Fatalf("remove %d: %v %v", want, removed, err)
		}
	}
	if got, err := s.listPrerequisites(ctx, "org", a); err != nil || len(got) != 1 || got[0].ID != c {
		t.Fatalf("after removing: %+v %v", got, err)
	}
}
