package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
)

const (
	topicA = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	topicB = "8b3e7f40-ac6d-4e5f-9a4b-3c4d5e6f7081"
)

// memoryStore keeps topics by id and prerequisite edges; it answers a cycle when the reverse edge exists.
type memoryStore struct {
	topics   map[string]Topic
	archived map[string]Topic
	edges    map[[2]string]bool
	broken   bool // every call fails as if the database were down
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		topics: map[string]Topic{
			topicA: {topicA, "Add fractions"},
			topicB: {topicB, "Compare fractions"},
		},
		archived: map[string]Topic{},
		edges:    map[[2]string]bool{},
	}
}

var errDown = errors.New("connection refused")

func (m *memoryStore) listTopics(_ context.Context, _ string, archived bool) ([]Topic, error) {
	from := m.topics
	if archived {
		from = m.archived
	}
	result := []Topic{}
	for _, v := range from {
		result = append(result, v)
	}
	slices.SortFunc(result, func(a, b Topic) int { return strings.Compare(a.Name, b.Name) })
	return result, nil
}

func (m *memoryStore) findTopic(_ context.Context, _, id string) (Topic, error) {
	if m.broken {
		return Topic{}, errDown
	}
	v, ok := m.topics[id]
	if !ok {
		return Topic{}, platform.ErrNotFound
	}
	return v, nil
}

func (m *memoryStore) insertTopic(_ context.Context, id, _, name string) error {
	if m.broken {
		return errDown
	}
	m.topics[id] = Topic{id, name}
	return nil
}

func (m *memoryStore) renameTopic(_ context.Context, _, id, name string) (bool, error) {
	if _, ok := m.topics[id]; !ok {
		return false, nil
	}
	m.topics[id] = Topic{id, name}
	return true, nil
}

func (m *memoryStore) archiveTopic(_ context.Context, _, id string) (bool, error) {
	if _, ok := m.topics[id]; !ok {
		return false, nil
	}
	m.archived[id] = m.topics[id]
	delete(m.topics, id)
	return true, nil
}

func (m *memoryStore) restoreTopic(_ context.Context, _, id string) (Topic, error) {
	v, ok := m.archived[id]
	if !ok {
		return Topic{}, platform.ErrNotFound
	}
	delete(m.archived, id)
	m.topics[id] = v
	return v, nil
}

func (m *memoryStore) insertPrerequisite(_ context.Context, _, id, prerequisite string) (prerequisiteResult, error) {
	if m.edges[[2]string{prerequisite, id}] {
		return prerequisiteCycle, nil
	}
	m.edges[[2]string{id, prerequisite}] = true
	return prerequisiteAdded, nil
}

func (m *memoryStore) listPrerequisites(_ context.Context, _, id string) ([]Topic, error) {
	result := []Topic{}
	for edge := range m.edges {
		if v, ok := m.topics[edge[1]]; ok && edge[0] == id {
			result = append(result, v)
		}
	}
	return result, nil
}

func (m *memoryStore) deletePrerequisite(_ context.Context, _, id, prerequisite string) (bool, error) {
	edge := [2]string{id, prerequisite}
	found := m.edges[edge]
	delete(m.edges, edge)
	return found, nil
}

func call(t *testing.T, store store, method, path, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store}.routes, method, path, body, "user", roles...)
}

func TestATopicIsJustANameAndTheResponseEchoesItTrimmed(t *testing.T) {
	store := newMemoryStore()
	cases := []struct {
		body   string
		status int
	}{
		{`{"name":"  Fractions "}`, http.StatusCreated},
		{`{"name":" "}`, http.StatusUnprocessableEntity},
		{`{"name":"Fractions","kind":"skill"}`, http.StatusUnprocessableEntity}, // levels are gone
		{`{"name":"Fractions","parent_id":null}`, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		w := call(t, store, http.MethodPost, "/v1/topics", tc.body, "teacher")
		if w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.body, w.Code, w.Body.String())
		}
		if w.Code == http.StatusCreated {
			var got Topic
			if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil || got.Name != "Fractions" || !platform.ID(got.ID) {
				t.Errorf("created %s, %v", w.Body.String(), err)
			}
		}
	}
	if w := call(t, store, http.MethodPost, "/v1/topics", `{"name":"Fractions"}`, "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student changed the curriculum: %d", w.Code)
	}
}

func TestRenameEchoesTheTrimmedName(t *testing.T) {
	store := newMemoryStore()
	w := call(t, store, http.MethodPatch, "/v1/topics/"+topicB, `{"name":" Halves "}`, "teacher")
	if w.Code != http.StatusOK || w.Body.String() != `{"id":"`+topicB+`","name":"Halves"}`+"\n" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w = call(t, store, http.MethodPatch, "/v1/topics/"+topicB[:35]+"9", `{"name":"Halves"}`, "teacher"); w.Code != http.StatusNotFound {
		t.Fatalf("renamed a missing topic: %d", w.Code)
	}
}

func TestPrerequisitesRejectSelfReferenceAndCycles(t *testing.T) {
	store := newMemoryStore()
	path := func(id string) string { return "/v1/topics/" + id + "/prerequisites" }
	if w := call(t, store, http.MethodPost, path(topicA), `{"prerequisite_id":"`+topicA+`"}`, "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("self prerequisite: %d", w.Code)
	}
	w := call(t, store, http.MethodPost, path(topicA), `{"prerequisite_id":"`+topicB+`"}`, "teacher")
	if w.Code != http.StatusOK || w.Body.String() != `{"prerequisite_id":"`+topicB+`","topic_id":"`+topicA+`"}`+"\n" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := call(t, store, http.MethodPost, path(topicB), `{"prerequisite_id":"`+topicA+`"}`, "teacher"); w.Code != http.StatusConflict {
		t.Fatalf("cycle accepted: %d", w.Code)
	}
}

func TestAFailedInsertIsAnInternalError(t *testing.T) {
	store := newMemoryStore()
	store.broken = true
	if w := call(t, store, http.MethodPost, "/v1/topics", `{"name":"Simplify"}`, "teacher"); w.Code != http.StatusInternalServerError {
		t.Fatalf("status %d", w.Code)
	}
}

func TestArchive(t *testing.T) {
	store := newMemoryStore()
	cases := []struct {
		id     string
		status int
	}{
		{"not-a-uuid", http.StatusNotFound},
		{topicB[:35] + "9", http.StatusNotFound},
		{topicA, http.StatusNoContent},
		{topicA, http.StatusNotFound}, // already archived
	}
	for _, tc := range cases {
		if w := call(t, store, http.MethodDelete, "/v1/topics/"+tc.id, "", "teacher"); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.id, w.Code, w.Body.String())
		}
	}
}

func TestAnArchivedTopicIsListedApartAndCanBeRestored(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodDelete, "/v1/topics/"+topicA, "", "teacher"); w.Code != http.StatusNoContent {
		t.Fatalf("archive: %d", w.Code)
	}
	list := func(query string, role string) string {
		t.Helper()
		w := call(t, store, http.MethodGet, "/v1/topics"+query, "", role)
		if w.Code != http.StatusOK {
			t.Fatalf("list %q: %d %s", query, w.Code, w.Body.String())
		}
		return strings.TrimSpace(w.Body.String())
	}
	archived := `[{"id":"` + topicA + `","name":"Add fractions"}]`
	if got := list("?archived=true", "student"); got != archived {
		t.Fatalf("archived list for a student: %s", got)
	}
	if got := list("", "teacher"); strings.Contains(got, topicA) {
		t.Fatalf("archived topic in the active list: %s", got)
	}
	if got := list("?archived=false", "teacher"); got != list("", "teacher") {
		t.Fatalf("archived=false differs from the default: %s", got)
	}
	if w := call(t, store, http.MethodGet, "/v1/topics?archived=yes", "", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("archived=yes: %d", w.Code)
	}

	if w := call(t, store, http.MethodPost, "/v1/topics/"+topicA+"/restore", "", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("student restore: %d", w.Code)
	}
	w := call(t, store, http.MethodPost, "/v1/topics/"+topicA+"/restore", "", "teacher")
	if w.Code != http.StatusOK || strings.TrimSpace(w.Body.String()) != `{"id":"`+topicA+`","name":"Add fractions"}` {
		t.Fatalf("restore: %d %s", w.Code, w.Body.String())
	}
	if got := list("?archived=true", "teacher"); got != "[]" {
		t.Fatalf("restored topic still archived: %s", got)
	}
	for _, id := range []string{topicA, topicB, "not-a-uuid"} { // now active, never archived, malformed
		if w := call(t, store, http.MethodPost, "/v1/topics/"+id+"/restore", "", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("restore %s: %d", id, w.Code)
		}
	}
}

func TestAMalformedPathIDIsNotFoundBeforeTheBodyIsRead(t *testing.T) {
	for _, tc := range [][2]string{{http.MethodPatch, "/v1/topics/x"}, {http.MethodPost, "/v1/topics/x/prerequisites"}} {
		if w := call(t, newMemoryStore(), tc[0], tc[1], "not json", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s %s: %d", tc[0], tc[1], w.Code)
		}
	}
}

func TestReadingATopic(t *testing.T) {
	if w := call(t, newMemoryStore(), http.MethodGet, "/v1/topics/"+topicA, "", "student"); w.Code != http.StatusOK || w.Body.String() != `{"id":"`+topicA+`","name":"Add fractions"}`+"\n" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	for _, id := range []string{topicB[:35] + "9", "not-a-uuid"} {
		if w := call(t, newMemoryStore(), http.MethodGet, "/v1/topics/"+id, "", "student"); w.Code != http.StatusNotFound {
			t.Fatalf("%s: status %d", id, w.Code)
		}
	}
	store := newMemoryStore()
	store.broken = true
	if w := call(t, store, http.MethodGet, "/v1/topics/"+topicA, "", "student"); w.Code != http.StatusInternalServerError {
		t.Fatalf("an outage answered %d", w.Code)
	}
}

func TestStudentsCannotChangeTheCurriculum(t *testing.T) {
	store := newMemoryStore()
	cases := [][3]string{
		{http.MethodPatch, "/v1/topics/" + topicB, `{"name":"Halves"}`},
		{http.MethodDelete, "/v1/topics/" + topicA, ""},
		{http.MethodPost, "/v1/topics/" + topicA + "/prerequisites", `{"prerequisite_id":"` + topicB + `"}`},
	}
	for _, tc := range cases {
		if w := call(t, store, tc[0], tc[1], tc[2], "student"); w.Code != http.StatusForbidden {
			t.Errorf("%s %s by a student: %d", tc[0], tc[1], w.Code)
		}
	}
	if _, ok := store.topics[topicA]; !ok || len(store.edges) != 0 || store.topics[topicB].Name != "Compare fractions" {
		t.Fatalf("the curriculum changed: %v %v", store.topics, store.edges)
	}
}

func TestPrerequisitesAreListedAndOnlyATeacherRemovesThem(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodPost, "/v1/topics/"+topicA+"/prerequisites", `{"prerequisite_id":"`+topicB+`"}`, "teacher"); w.Code != http.StatusOK {
		t.Fatalf("add: %d %s", w.Code, w.Body.String())
	}
	list := "/v1/topics/" + topicA + "/prerequisites"
	if w := call(t, store, http.MethodGet, list, "", "student"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "Compare fractions") {
		t.Fatalf("list: %d %s", w.Code, w.Body.String())
	}
	edge := list + "/" + topicB
	if w := call(t, store, http.MethodDelete, edge, "", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student removed a prerequisite: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, edge, "", "teacher"); w.Code != http.StatusNoContent {
		t.Fatalf("remove: %d %s", w.Code, w.Body.String())
	}
	for _, path := range []string{edge, list + "/not-an-id"} {
		if w := call(t, store, http.MethodDelete, path, "", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s: %d", path, w.Code)
		}
	}
	if w := call(t, store, http.MethodGet, "/v1/topics/"+topicB+"0/prerequisites", "", "teacher"); w.Code != http.StatusNotFound {
		t.Errorf("an unknown topic's prerequisites: %d", w.Code)
	}
}
