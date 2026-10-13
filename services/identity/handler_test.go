package main

import (
	"context"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

const room = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"

// memoryStore is an in-memory store: one organization's users, classrooms and enrollments.
type memoryStore struct {
	roles      map[string]string   // user -> role
	owners     map[string]string   // classroom -> teacher
	enrolled   map[string][]string // classroom -> students
	registered []string
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		roles:    map[string]string{"teacher-1": "teacher", "student-1": "student", "student-2": "student"},
		owners:   map[string]string{room: "teacher-1"},
		enrolled: map[string][]string{room: {"student-1"}},
	}
}

func (m *memoryStore) registerUser(_ context.Context, _, subject, role string) error {
	m.roles[subject] = role
	m.registered = append(m.registered, subject+":"+role)
	return nil
}

func (m *memoryStore) findClassroomOwner(_ context.Context, classroom string) (string, string, error) {
	teacher, ok := m.owners[classroom]
	if !ok {
		return "", "", pgx.ErrNoRows
	}
	return "org", teacher, nil
}

func (m *memoryStore) isEnrolled(_ context.Context, classroom, student string) (bool, error) {
	for _, s := range m.enrolled[classroom] {
		if s == student {
			return true, nil
		}
	}
	return false, nil
}

func (m *memoryStore) insertClassroom(_ context.Context, id, _, _, teacher string) error {
	m.owners[id] = teacher
	return nil
}

func (m *memoryStore) listClassrooms(context.Context, string, string, bool) ([]Classroom, error) {
	return []Classroom{}, nil
}

func (m *memoryStore) studentExists(_ context.Context, student, _ string) (bool, error) {
	return m.roles[student] == "student", nil
}

func (m *memoryStore) insertEnrollment(_ context.Context, classroom, student string) error {
	m.enrolled[classroom] = append(m.enrolled[classroom], student)
	return nil
}

// call sends a request as a verified caller with the given roles.
// call sends a request as a verified caller with the given roles.
func call(t *testing.T, store store, method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store}.routes, method, path, body, subject, roles...)
}

func TestRegisterRecordsTheTeacherRoleFirst(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodPost, "/v1/me", "", "dual", "student", "teacher"); w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/me", "", "service"); w.Code != 403 {
		t.Fatalf("a caller without a person role registered: %d", w.Code)
	}
	if len(store.registered) != 1 || store.registered[0] != "dual:teacher" {
		t.Fatalf("registered %v", store.registered)
	}
}

func TestAccessAnswersTheRelation(t *testing.T) {
	store := newMemoryStore()
	cases := []struct {
		subject, role, path string
		status              int
		relation            string
	}{
		{"teacher-1", "teacher", "/access", 200, `"relation":"teacher"`},
		{"student-1", "student", "/access", 200, `"relation":"student"`},
		{"student-2", "student", "/access", 403, ""},
		{"teacher-1", "teacher", "/access?student_id=student-2", 403, ""},
	}
	for _, tc := range cases {
		w := call(t, store, http.MethodGet, "/v1/classrooms/"+room+tc.path, "", tc.subject, tc.role)
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.relation) {
			t.Errorf("%s %s: %d %s", tc.subject, tc.path, w.Code, w.Body.String())
		}
	}
}

func TestCreateValidatesTheName(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"  "}`, "teacher-1", "teacher"); w.Code != 422 {
		t.Fatalf("blank name: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "student-1", "student"); w.Code != 403 {
		t.Fatalf("student created a classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "teacher-1", "teacher"); w.Code != 201 {
		t.Fatalf("status %d", w.Code)
	}
}

func TestOnlyTheOwnerEnrollsKnownStudents(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	if w := call(t, store, http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-2", "teacher"); w.Code != 403 {
		t.Fatalf("another teacher enrolled: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path, `{"student_id":"stranger"}`, "teacher-1", "teacher"); w.Code != 422 {
		t.Fatalf("unknown student: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-1", "teacher"); w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-2"); !enrolled {
		t.Fatal("student-2 was not enrolled")
	}
}
