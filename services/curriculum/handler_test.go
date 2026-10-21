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

// memorystore keeps topics by id and prerequisite edges; it answers a cycle when the reverse edge exists.
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

func (m *memoryStore) listTopics(_ context.Context, _ string, archived bool, paging ...platform.ListWindow) ([]Topic, error) {
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

// step is one request of a scenario and the answer it expects: its status and, when want is set, a part of
// the body.
type step struct {
	name, method, path, body, role string
	status                         int
	want                           string
}

// run sends the steps in order to the same store and stops at the first answer that is not the one
// expected.
func run(t *testing.T, store store, steps ...step) {
	t.Helper()
	for _, s := range steps {
		if w := call(t, store, s.method, s.path, s.body, s.role); w.Code != s.status || !strings.Contains(w.Body.String(), s.want) {
			t.Fatalf("%s: %d %s", s.name, w.Code, w.Body.String())
		}
	}
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
	run(t, store, step{"a student changed the curriculum", http.MethodPost, "/v1/topics", `{"name":"Fractions"}`, "student", http.StatusForbidden, ""})
}

func TestRenameEchoesTheTrimmedName(t *testing.T) {
	run(t, newMemoryStore(),
		step{"rename", http.MethodPatch, "/v1/topics/" + topicB, `{"name":" Halves "}`, "teacher", http.StatusOK, `{"id":"` + topicB + `","name":"Halves"}`},
		step{"renamed a missing topic", http.MethodPatch, "/v1/topics/" + topicB[:35] + "9", `{"name":"Halves"}`, "teacher", http.StatusNotFound, ""},
	)
}

func TestPrerequisitesRejectSelfReferenceAndCycles(t *testing.T) {
	path := func(id string) string { return "/v1/topics/" + id + "/prerequisites" }
	run(t, newMemoryStore(),
		step{"self prerequisite", http.MethodPost, path(topicA), `{"prerequisite_id":"` + topicA + `"}`, "teacher", http.StatusUnprocessableEntity, ""},
		step{"add", http.MethodPost, path(topicA), `{"prerequisite_id":"` + topicB + `"}`, "teacher", http.StatusOK, `{"prerequisite_id":"` + topicB + `","topic_id":"` + topicA + `"}`},
		step{"cycle accepted", http.MethodPost, path(topicB), `{"prerequisite_id":"` + topicA + `"}`, "teacher", http.StatusConflict, ""},
	)
}

func TestAFailedInsertIsAnInternalError(t *testing.T) {
	store := newMemoryStore()
	store.broken = true
	run(t, store, step{"insert", http.MethodPost, "/v1/topics", `{"name":"Simplify"}`, "teacher", http.StatusInternalServerError, ""})
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
	restore := func(id string) string { return "/v1/topics/" + id + "/restore" }
	run(t, store,
		step{"archived=yes", http.MethodGet, "/v1/topics?archived=yes", "", "teacher", http.StatusUnprocessableEntity, ""},
		step{"student restore", http.MethodPost, restore(topicA), "", "student", http.StatusForbidden, ""},
		step{"restore", http.MethodPost, restore(topicA), "", "teacher", http.StatusOK, `{"id":"` + topicA + `","name":"Add fractions"}`},
	)
	if got := list("?archived=true", "teacher"); got != "[]" {
		t.Fatalf("restored topic still archived: %s", got)
	}
	run(t, store,
		step{"restore an active topic", http.MethodPost, restore(topicA), "", "teacher", http.StatusNotFound, ""},
		step{"restore one never archived", http.MethodPost, restore(topicB), "", "teacher", http.StatusNotFound, ""},
		step{"restore a malformed id", http.MethodPost, restore("not-a-uuid"), "", "teacher", http.StatusNotFound, ""},
	)
}

func TestAMalformedPathIDIsNotFoundBeforeTheBodyIsRead(t *testing.T) {
	for _, tc := range [][2]string{{http.MethodPatch, "/v1/topics/x"}, {http.MethodPost, "/v1/topics/x/prerequisites"}} {
		if w := call(t, newMemoryStore(), tc[0], tc[1], "not json", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s %s: %d", tc[0], tc[1], w.Code)
		}
	}
}

func TestReadingATopic(t *testing.T) {
	run(t, newMemoryStore(),
		step{"read", http.MethodGet, "/v1/topics/" + topicA, "", "student", http.StatusOK, `{"id":"` + topicA + `","name":"Add fractions"}`},
		step{"an unknown topic", http.MethodGet, "/v1/topics/" + topicB[:35] + "9", "", "student", http.StatusNotFound, ""},
		step{"a malformed id", http.MethodGet, "/v1/topics/not-a-uuid", "", "student", http.StatusNotFound, ""},
	)
	store := newMemoryStore()
	store.broken = true
	run(t, store, step{"an outage", http.MethodGet, "/v1/topics/" + topicA, "", "student", http.StatusInternalServerError, ""})
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
	list := "/v1/topics/" + topicA + "/prerequisites"
	edge := list + "/" + topicB
	run(t, newMemoryStore(),
		step{"add", http.MethodPost, list, `{"prerequisite_id":"` + topicB + `"}`, "teacher", http.StatusOK, ""},
		step{"list", http.MethodGet, list, "", "student", http.StatusOK, "Compare fractions"},
		step{"a student removed a prerequisite", http.MethodDelete, edge, "", "student", http.StatusForbidden, ""},
		step{"remove", http.MethodDelete, edge, "", "teacher", http.StatusNoContent, ""},
		step{"remove again", http.MethodDelete, edge, "", "teacher", http.StatusNotFound, ""},
		step{"remove a malformed id", http.MethodDelete, list + "/not-an-id", "", "teacher", http.StatusNotFound, ""},
		step{"an unknown topic's prerequisites", http.MethodGet, "/v1/topics/" + topicB + "0/prerequisites", "", "teacher", http.StatusNotFound, ""},
	)
}

func (m *memoryStore) countTopics(ctx context.Context, org string, archived bool) (int, error) {
	items, err := m.listTopics(ctx, org, archived)
	return len(items), err
}
