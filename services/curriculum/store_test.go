package main

import (
	"context"
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

// node inserts an active node and returns its id.
func node(t *testing.T, s pgStore, org, kind string, parent *string) string {
	t.Helper()
	id := uuid.NewString()
	if err := s.insertNode(context.Background(), id, org, kind, kind+" "+id[:4], parent); err != nil {
		t.Fatal(err)
	}
	return id
}

func TestTheCycleCheckFollowsTheWholeChain(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	a, b, c := node(t, s, "org", "skill", nil), node(t, s, "org", "skill", nil), node(t, s, "org", "skill", nil)
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

func TestPrerequisitesStayInTheOrganizationAndBetweenActiveSkills(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	mine, theirs := node(t, s, "org", "skill", nil), node(t, s, "elsewhere", "skill", nil)
	topic := node(t, s, "org", "topic", nil)
	retired := node(t, s, "org", "skill", nil)
	if got, err := s.archiveNode(ctx, "org", retired); err != nil || got != archived {
		t.Fatal(got, err)
	}
	for _, edge := range [][2]string{{mine, theirs}, {theirs, mine}, {mine, topic}, {mine, retired}} {
		if got, err := s.insertPrerequisite(ctx, "org", edge[0], edge[1]); err != nil || got != prerequisiteInvalid {
			t.Errorf("%v: %v %v", edge, got, err)
		}
	}
	// Through the handler, as the other organization's teacher would call it.
	server := server{store: s}.routes
	w := platformtest.Serve(server, http.MethodPost, "/v1/skills/"+mine+"/prerequisites", `{"prerequisite_id":"`+theirs+`"}`, "teacher-1", "teacher")
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("cross-organization prerequisite: %d %s", w.Code, w.Body.String())
	}
}

func TestAParentFromAnotherOrganizationIsRefused(t *testing.T) {
	s := testStore(t)
	theirs, mine := node(t, s, "elsewhere", "topic", nil), node(t, s, "org", "topic", nil)
	routes := server{store: s}.routes
	w := platformtest.Serve(routes, http.MethodPost, "/v1/nodes", `{"kind":"skill","name":"Halves","parent_id":"`+theirs+`"}`, "teacher-1", "teacher")
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("cross-organization parent: %d %s", w.Code, w.Body.String())
	}
	w = platformtest.Serve(routes, http.MethodPost, "/v1/nodes", `{"kind":"skill","name":"Halves","parent_id":"`+mine+`"}`, "teacher-1", "teacher")
	if w.Code != http.StatusCreated {
		t.Fatalf("parent in the organization: %d %s", w.Code, w.Body.String())
	}
	if nodes, err := s.listNodes(context.Background(), "org"); err != nil || len(nodes) != 2 {
		t.Fatalf("nodes = %v, %v", nodes, err)
	}
}

func TestOtherOrganizationsNodesCannotBeReadRenamedOrArchived(t *testing.T) {
	ctx, s := context.Background(), testStore(t)
	parent := node(t, s, "org", "topic", nil)
	child := node(t, s, "org", "skill", &parent)
	if _, err := s.findNode(ctx, "elsewhere", child); err == nil {
		t.Error("found another organization's node")
	}
	if ok, err := s.renameNode(ctx, "elsewhere", child, "Taken"); err != nil || ok {
		t.Errorf("renamed another organization's node: %v %v", ok, err)
	}
	if got, err := s.archiveNode(ctx, "elsewhere", child); err != nil || got != archiveMissing {
		t.Errorf("archive from another organization: %v %v", got, err)
	}
	if got, err := s.archiveNode(ctx, "org", parent); err != nil || got != archiveHasChildren {
		t.Errorf("archive with an active child: %v %v", got, err)
	}
	if ok, err := s.renameNode(ctx, "org", child, "Halves"); err != nil || !ok {
		t.Fatalf("rename: %v %v", ok, err)
	}
	if n, err := s.findNode(ctx, "org", child); err != nil || n.Name != "Halves" || n.Parent == nil || *n.Parent != parent {
		t.Fatalf("node = %v, %v", n, err)
	}
	for _, id := range []string{child, parent} {
		if got, err := s.archiveNode(ctx, "org", id); err != nil || got != archived {
			t.Fatalf("archive %s: %v %v", id, got, err)
		}
	}
	if kind, err := s.findKind(ctx, "org", parent); err == nil {
		t.Fatalf("an archived node is still found: %s", kind)
	}
}
