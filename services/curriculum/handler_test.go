package main

import (
	"context"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"testing"
)

const (
	topicID = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"
	skillA  = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	skillB  = "8b3e7f40-ac6d-4e5f-9a4b-3c4d5e6f7081"
)

// memoryStore keeps nodes by id and prerequisite edges; it answers a cycle when the reverse edge exists.
type memoryStore struct {
	nodes map[string]Node
	edges map[[2]string]bool
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		nodes: map[string]Node{
			topicID: {topicID, "topic", "Fractions", nil},
			skillA:  {skillA, "skill", "Add fractions", nil},
			skillB:  {skillB, "skill", "Compare fractions", nil},
		},
		edges: map[[2]string]bool{},
	}
}

func (m *memoryStore) listNodes(context.Context, string) ([]Node, error) { return nil, nil }

func (m *memoryStore) findNode(_ context.Context, _, id string) (Node, error) {
	n, ok := m.nodes[id]
	if !ok {
		return Node{}, platform.ErrNotFound
	}
	return n, nil
}

func (m *memoryStore) findKind(ctx context.Context, org, id string) (string, error) {
	n, err := m.findNode(ctx, org, id)
	return n.Kind, err
}

func (m *memoryStore) insertNode(_ context.Context, id, _, kind, name string, parent *string) error {
	m.nodes[id] = Node{id, kind, name, parent}
	return nil
}

func (m *memoryStore) renameNode(context.Context, string, string, string) (bool, error) {
	return true, nil
}

func (m *memoryStore) archiveNode(context.Context, string, string) (bool, error) { return true, nil }

func (m *memoryStore) insertPrerequisite(_ context.Context, _, id, prerequisite string) (prerequisiteResult, error) {
	if m.edges[[2]string{prerequisite, id}] {
		return prerequisiteCycle, nil
	}
	m.edges[[2]string{id, prerequisite}] = true
	return prerequisiteAdded, nil
}

func call(t *testing.T, store store, method, path, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store}.routes, method, path, body, "user", roles...)
}

func TestANodeNeedsTheLevelAboveAsParent(t *testing.T) {
	store := newMemoryStore()
	cases := []struct {
		body   string
		status int
	}{
		{`{"kind":"skill","name":"Simplify","parent_id":"` + topicID + `"}`, http.StatusCreated},
		{`{"kind":"unit","name":"Number","parent_id":"` + topicID + `"}`, http.StatusUnprocessableEntity},
		{`{"kind":"skill","name":"Simplify","parent_id":"` + skillA + `"}`, http.StatusUnprocessableEntity},
		{`{"kind":"chapter","name":"Simplify"}`, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		if w := call(t, store, http.MethodPost, "/v1/nodes", tc.body, "teacher"); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.body, w.Code, w.Body.String())
		}
	}
	if w := call(t, store, http.MethodPost, "/v1/nodes", cases[0].body, "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student changed the curriculum: %d", w.Code)
	}
}

func TestPrerequisitesRejectSelfReferenceAndCycles(t *testing.T) {
	store := newMemoryStore()
	path := func(id string) string { return "/v1/skills/" + id + "/prerequisites" }
	if w := call(t, store, http.MethodPost, path(skillA), `{"prerequisite_id":"`+skillA+`"}`, "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("self prerequisite: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path(skillA), `{"prerequisite_id":"`+skillB+`"}`, "teacher"); w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path(skillB), `{"prerequisite_id":"`+skillA+`"}`, "teacher"); w.Code != http.StatusConflict {
		t.Fatalf("cycle accepted: %d", w.Code)
	}
}

func TestAMissingNodeIsNotFound(t *testing.T) {
	if w := call(t, newMemoryStore(), http.MethodGet, "/v1/nodes/"+skillB[:35]+"9", "", "student"); w.Code != http.StatusNotFound {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, newMemoryStore(), http.MethodGet, "/v1/nodes/not-a-uuid", "", "student"); w.Code != http.StatusNotFound {
		t.Fatalf("status %d", w.Code)
	}
}
