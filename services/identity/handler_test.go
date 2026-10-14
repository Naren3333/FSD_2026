package main

import (
	"context"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

const room = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"

// memoryStore is an in-memory store: one organization's users, classrooms and enrollments.
type memoryStore struct {
	roles      map[string]string   // user -> role
	usernames  map[string]string   // user -> username
	owners     map[string]string   // classroom -> teacher
	enrolled   map[string][]string // classroom -> students
	registered []string
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		roles:     map[string]string{"teacher-1": "teacher", "student-1": "student", "student-2": "student", "student-3": "student"},
		usernames: map[string]string{"student-1": "ada", "student-2": "grace", "student-3": "Grace"},
		owners:    map[string]string{room: "teacher-1"},
		enrolled:  map[string][]string{room: {"student-1"}},
	}
}

func (m *memoryStore) registerUser(_ context.Context, _, subject, role, username, name string) error {
	m.roles[subject] = role
	m.registered = append(m.registered, subject+":"+role+":"+username+":"+name)
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

func (m *memoryStore) studentsNamed(_ context.Context, _, username string) ([]string, error) {
	ids := []string{}
	for id, name := range m.usernames {
		if strings.EqualFold(name, username) && m.roles[id] == "student" {
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
	for id, role := range m.roles {
		if role == "student" && m.usernames[id] != "" && strings.Contains(strings.ToLower(m.usernames[id]), strings.ToLower(search)) {
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
	if w := call(t, store, http.MethodPost, "/v1/me", "", "dual", "student", "teacher"); w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, store, http.MethodPost, "/v1/me", "", "service"); w.Code != 403 {
		t.Fatalf("a caller without a person role registered: %d", w.Code)
	}
	if len(store.registered) != 1 || store.registered[0] != "dual:teacher::" {
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

func TestRegisterRecordsTheNamesFromTheToken(t *testing.T) {
	store := newMemoryStore()
	var c platform.Claims
	c.Subject, c.Org, c.Username, c.Name, c.Realm.Roles = "s9", "org", " lin ", "  ", []string{"student"}
	r := httptest.NewRequestWithContext(platform.WithClaims(context.Background(), c), http.MethodPost, "/v1/me", strings.NewReader(""))
	w := httptest.NewRecorder()
	mux := http.NewServeMux()
	server{store: store}.routes(mux)
	mux.ServeHTTP(w, r)
	// Without a full name, the username stands in for it.
	if w.Code != 200 || store.registered[0] != "s9:student:lin:lin" || !strings.Contains(w.Body.String(), `"username":"lin"`) {
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
		{`{"username":"ADA"}`, 200},
		{`{"username":"grace"}`, 422}, // two students share it
		{`{"username":"nobody"}`, 422},
		{`{}`, 422},
		{`{"student_id":"student-2","username":"ada"}`, 422},
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
	if w := call(t, store, http.MethodGet, roster, "", "student-1", "student"); w.Code != 403 {
		t.Fatalf("a student read the roster: %d", w.Code)
	}
	if w := call(t, store, http.MethodGet, roster, "", "teacher-2", "teacher"); w.Code != 403 {
		t.Fatalf("another teacher read the roster: %d", w.Code)
	}
	if w := call(t, store, http.MethodGet, roster, "", "teacher-1", "teacher"); w.Code != 200 || !strings.Contains(w.Body.String(), `"username":"ada"`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	remove := "/v1/classrooms/" + room + "/enrollments/student-1"
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-2", "teacher"); w.Code != 403 {
		t.Fatalf("another teacher removed a student: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-1", "teacher"); w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Fatal("student-1 is still enrolled")
	}
	if w := call(t, store, http.MethodDelete, remove, "", "teacher-1", "teacher"); w.Code != 404 {
		t.Fatalf("removing twice: %d", w.Code)
	}
}

func TestOnlyTheOwnerDeletesAClassroom(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room
	if w := call(t, store, http.MethodDelete, path, "", "student-1", "student"); w.Code != 403 {
		t.Fatalf("a student deleted the classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, path, "", "teacher-2", "teacher"); w.Code != 403 {
		t.Fatalf("another teacher deleted the classroom: %d", w.Code)
	}
	if w := call(t, store, http.MethodDelete, path, "", "teacher-1", "teacher"); w.Code != 200 {
		t.Fatalf("status %d", w.Code)
	}
	// Once deleted, nobody has access, including its teacher and its students.
	for _, caller := range [][2]string{{"teacher-1", "teacher"}, {"student-1", "student"}} {
		if w := call(t, store, http.MethodGet, path+"/access", "", caller[0], caller[1]); w.Code != 403 {
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
	if w := call(t, store, http.MethodGet, "/v1/students", "", "student-1", "student"); w.Code != 403 {
		t.Fatalf("a student listed every student: %d", w.Code)
	}
	cases := []struct {
		query, want string
		status      int
	}{
		{"", `"total":3`, 200},
		{"?search=GRACE", `"total":2`, 200},
		{"?offset=2&limit=1", `"students":[{"id":"student-3"`, 200},
		{"?offset=5", `"students":[],"total":3`, 200},
		{"?limit=0", "", 422},
		{"?limit=51", "", 422},
		{"?offset=-1", "", 422},
		{"?offset=x", "", 422},
	}
	for _, tc := range cases {
		w := call(t, store, http.MethodGet, "/v1/students"+tc.query, "", "teacher-1", "teacher")
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.want) {
			t.Errorf("%s: %d %s", tc.query, w.Code, w.Body.String())
		}
	}
}
