package main

import (
	"context"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"
)

const room = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"

// memoryStore is an in-memory store: one organization's users, classrooms and enrollments.
type memoryStore struct {
	roles      map[string]string   // user -> role
	students   map[string]bool     // users who may be enrolled: students and dual-role accounts
	usernames  map[string]string   // user -> username
	owners     map[string]string   // classroom -> teacher
	enrolled   map[string][]string // classroom -> students
	registered []string
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		roles:     map[string]string{"teacher-1": "teacher", "student-1": "student", "student-2": "student", "student-3": "student", "dual-1": "teacher"},
		students:  map[string]bool{"student-1": true, "student-2": true, "student-3": true, "dual-1": true},
		usernames: map[string]string{"student-1": "ada", "student-2": "grace", "student-3": "Grace", "dual-1": "lin"},
		owners:    map[string]string{room: "teacher-1"},
		enrolled:  map[string][]string{room: {"student-1"}},
	}
}

func (m *memoryStore) registerUser(_ context.Context, _, subject, role string, student bool, username, name string) error {
	m.roles[subject] = role
	m.students[subject] = student
	m.registered = append(m.registered, subject+":"+role+":"+username+":"+name)
	return nil
}

func (m *memoryStore) findClassroomOwner(_ context.Context, classroom string) (string, string, error) {
	teacher, ok := m.owners[classroom]
	if !ok {
		return "", "", platform.ErrNotFound
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

func (m *memoryStore) insertClassroom(_ context.Context, id, name, _, teacher string) error {
	switch {
	case m.roles[teacher] == "":
		return errProfileRequired
	case name == "db down":
		return errors.New("connection refused")
	}
	m.owners[id] = teacher
	return nil
}

func (m *memoryStore) listClassrooms(context.Context, string, string, bool) ([]Classroom, error) {
	return []Classroom{}, nil
}

func (m *memoryStore) studentExists(_ context.Context, student, _ string) (bool, error) {
	return m.students[student], nil
}

func (m *memoryStore) insertEnrollment(_ context.Context, classroom, student string) error {
	if _, ok := m.owners[classroom]; !ok {
		return platform.ErrNotFound
	}
	m.enrolled[classroom] = append(m.enrolled[classroom], student)
	return nil
}

func (m *memoryStore) studentsNamed(_ context.Context, _, username string) ([]string, error) {
	ids := []string{}
	for id, name := range m.usernames {
		if strings.EqualFold(name, username) && m.students[id] {
			ids = append(ids, id)
		}
	}
	return ids, nil
}

func (m *memoryStore) removeEnrollment(_ context.Context, classroom, student string) (bool, error) {
	kept := []string{}
	for _, s := range m.enrolled[classroom] {
		if s != student {
			kept = append(kept, s)
		}
	}
	removed := len(kept) < len(m.enrolled[classroom])
	m.enrolled[classroom] = kept
	return removed, nil
}

func (m *memoryStore) listStudents(_ context.Context, classroom string) ([]Student, error) {
	result := []Student{}
	for _, id := range m.enrolled[classroom] {
		result = append(result, Student{ID: id, Username: m.usernames[id], Name: m.usernames[id]})
	}
	return result, nil
}

func (m *memoryStore) deleteClassroom(_ context.Context, classroom, _ string) error {
	delete(m.owners, classroom)
	delete(m.enrolled, classroom)
	return nil
}

func (m *memoryStore) listOrgStudents(_ context.Context, _, search string, offset, limit int) ([]Student, int, error) {
	matching := []Student{}
	for id, student := range m.students {
		if student && m.usernames[id] != "" && strings.Contains(strings.ToLower(m.usernames[id]), strings.ToLower(search)) {
			matching = append(matching, Student{ID: id, Username: m.usernames[id]})
		}
	}
	sort.Slice(matching, func(i, j int) bool { return matching[i].ID < matching[j].ID })
	end := min(offset+limit, len(matching))
	return matching[min(offset, end):end], len(matching), nil
}

// call sends a request as a verified caller with the given roles.
func call(t *testing.T, store store, method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store}.routes, method, path, body, subject, roles...)
}

func TestRegisterRecordsTheTeacherRoleFirst(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodPost, "/v1/me", "", "dual", "student", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/me", "", "service"); w.Code != http.StatusForbidden {
		t.Fatalf("a caller without a person role registered: %d", w.Code)
	}
	if len(store.registered) != 1 || store.registered[0] != "dual:teacher::" || !store.students["dual"] {
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
		{"teacher-1", "teacher", "/access", http.StatusOK, `"relation":"teacher"`},
		{"student-1", "student", "/access", http.StatusOK, `"relation":"student"`},
		{"student-2", "student", "/access", http.StatusForbidden, ""},
		{"teacher-1", "teacher", "/access?student_id=student-2", http.StatusForbidden, ""},
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
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"  "}`, "teacher-1", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("blank name: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("student created a classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":" Year 5 "}`, "teacher-1", "teacher"); w.Code != http.StatusCreated || !strings.Contains(w.Body.String(), `"name":"Year 5"`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	// The limit counts characters, not bytes.
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"`+strings.Repeat("é", maxName)+`"}`, "teacher-1", "teacher"); w.Code != http.StatusCreated {
		t.Fatalf("%d characters: %d", maxName, w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"`+strings.Repeat("é", maxName+1)+`"}`, "teacher-1", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("%d characters: %d", maxName+1, w.Code)
	}
}

func TestCreateTellsAMissingProfileFromAFailure(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "teacher-9", "teacher"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "PROFILE_REQUIRED") {
		t.Fatalf("unregistered teacher: %d %s", w.Code, w.Body.String())
	}
	if w := call(t, store, http.MethodPost, "/v1/classrooms", `{"name":"db down"}`, "teacher-1", "teacher"); w.Code != http.StatusInternalServerError {
		t.Fatalf("database failure: %d", w.Code)
	}
}

func TestDualRoleAccountsCanBeEnrolledButNotInTheirOwnClassroom(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	if w := call(t, store, http.MethodPost, path, `{"username":"lin"}`, "teacher-1", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("dual-role account by username: %d %s", w.Code, w.Body.String())
	}
	if w := call(t, store, http.MethodPost, path, `{"student_id":"dual-1"}`, "teacher-1", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("dual-role account by id: %d %s", w.Code, w.Body.String())
	}
	store.owners[room] = "dual-1"
	if w := call(t, store, http.MethodPost, path, `{"student_id":"dual-1"}`, "dual-1", "teacher", "student"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("a teacher enrolled in their own classroom: %d", w.Code)
	}
}

func TestOnlyTheOwnerEnrollsKnownStudents(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	if w := call(t, store, http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-2", "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("another teacher enrolled: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path, `{"student_id":"stranger"}`, "teacher-1", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("unknown student: %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-1", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-2"); !enrolled {
		t.Fatal("student-2 was not enrolled")
	}
}

func TestRegisterRecordsTheNamesFromTheToken(t *testing.T) {
	store := newMemoryStore()
	c := platformtest.Claims("s9", "student")
	c.Username, c.Name = " lin ", "  "
	w := platformtest.ServeRequest(server{store: store}.routes, platformtest.Request(c, http.MethodPost, "/v1/me", ""))
	// Without a full name, the username stands in for it.
	if w.Code != http.StatusOK || store.registered[0] != "s9:student:lin:lin" || !strings.Contains(w.Body.String(), `"username":"lin"`) {
		t.Fatalf("%d %v", w.Code, store.registered)
	}
}

func TestEnrollByUsername(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	cases := []struct {
		body   string
		status int
	}{
		{`{"username":"ADA"}`, http.StatusOK},
		{`{"username":"grace"}`, http.StatusUnprocessableEntity}, // two students share it
		{`{"username":"nobody"}`, http.StatusUnprocessableEntity},
		{`{}`, http.StatusUnprocessableEntity},
		{`{"student_id":"student-2","username":"ada"}`, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		if w := call(t, store, http.MethodPost, path, tc.body, "teacher-1", "teacher"); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.body, w.Code, w.Body.String())
		}
	}
}

func TestOnlyTheOwnerSeesTheRosterAndRemovesStudents(t *testing.T) {
	store := newMemoryStore()
	roster := "/v1/classrooms/" + room + "/students"
	if w := call(t, store, http.MethodGet, roster, "", "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student read the roster: %d", w.Code)
	}
	if w := call(t, store, http.MethodGet, roster, "", "teacher-2", "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("another teacher read the roster: %d", w.Code)
	}
	if w := call(t, store, http.MethodGet, roster, "", "teacher-1", "teacher"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"username":"ada"`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	remove := "/v1/classrooms/" + room + "/enrollments/student-1"
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-2", "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("another teacher removed a student: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-1", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Fatal("student-1 is still enrolled")
	}
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-1", "teacher"); w.Code != http.StatusNotFound {
		t.Fatalf("removing twice: %d", w.Code)
	}
}

func TestOnlyTheOwnerDeletesAClassroom(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room
	if w := call(t, store, http.MethodDelete, path, "", "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student deleted the classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, path, "", "teacher-2", "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("another teacher deleted the classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, path, "", "teacher-1", "teacher"); w.Code != http.StatusOK {
		t.Fatalf("status %d", w.Code)
	}
	// Once deleted, nobody has access, including its teacher and its students.
	for _, caller := range [][2]string{{"teacher-1", "teacher"}, {"student-1", "student"}} {
		if w := call(t, store, http.MethodGet, path+"/access", "", caller[0], caller[1]); w.Code != http.StatusForbidden {
			t.Errorf("%s still has access: %d", caller[0], w.Code)
		}
	}
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Error("student-1 is still enrolled in the deleted classroom")
	}
	if store.roles["student-1"] != "student" {
		t.Error("deleting the classroom removed student-1's account")
	}
}

func TestOnlyTeachersListTheSchoolsStudents(t *testing.T) {
	store := newMemoryStore()
	if w := call(t, store, http.MethodGet, "/v1/students", "", "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student listed every student: %d", w.Code)
	}
	cases := []struct {
		query, want string
		status      int
	}{
		{"", `"total":4`, http.StatusOK},
		{"?search=GRACE", `"total":2`, http.StatusOK},
		{"?offset=3&limit=1", `"students":[{"id":"student-3"`, http.StatusOK},
		{"?offset=5", `"students":[],"total":4`, http.StatusOK},
		{"?limit=0", "", http.StatusUnprocessableEntity},
		{"?limit=51", "", http.StatusUnprocessableEntity},
		{"?offset=-1", "", http.StatusUnprocessableEntity},
		{"?offset=x", "", http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		w := call(t, store, http.MethodGet, "/v1/students"+tc.query, "", "teacher-1", "teacher")
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.want) {
			t.Errorf("%s: %d %s", tc.query, w.Code, w.Body.String())
		}
	}
}
