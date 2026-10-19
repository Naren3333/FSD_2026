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
	usernames  map[string]string   // user -> username
	owners     map[string]string   // classroom -> teacher
	enrolled   map[string][]string // classroom -> students
	registered []string
	deleted    []string
}

func newMemoryStore() *memoryStore {
	return &memoryStore{
		roles:     map[string]string{"teacher-1": "teacher", "teacher-2": "teacher", "student-1": "student", "student-2": "student", "student-3": "student"},
		usernames: map[string]string{"teacher-2": "lin", "student-1": "ada", "student-2": "grace", "student-3": "Grace"},
		owners:    map[string]string{room: "teacher-1"},
		enrolled:  map[string][]string{room: {"student-1"}},
	}
}

func (m *memoryStore) registerUser(_ context.Context, _, subject, role, username, name string) error {
	if stored, ok := m.roles[subject]; ok && stored != role {
		return errRoleChanged
	}
	m.roles[subject] = role
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

func (m *memoryStore) listClassrooms(context.Context, string, string, bool, ...platform.ListWindow) ([]Classroom, error) {
	return []Classroom{}, nil
}

func (m *memoryStore) studentExists(_ context.Context, student, _ string) (bool, error) {
	return m.roles[student] == "student", nil
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
		if strings.EqualFold(name, username) && m.roles[id] == "student" {
			ids = append(ids, id)
		}
	}
	return ids, nil
}

func (m *memoryStore) removeEnrollment(_ context.Context, classroom, _, student string) (bool, error) {
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

func (m *memoryStore) listStudents(_ context.Context, classroom string, offset, limit int) ([]Student, int, error) {
	result := []Student{}
	enrolled := m.enrolled[classroom]
	for _, id := range enrolled[min(offset, len(enrolled)):min(offset+limit, len(enrolled))] {
		result = append(result, Student{ID: id, Username: m.usernames[id], Name: m.usernames[id]})
	}
	return result, len(enrolled), nil
}

func (m *memoryStore) listStudentIDs(_ context.Context, classroom string) ([]string, error) {
	return append([]string{}, m.enrolled[classroom]...), nil
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

func (m *memoryStore) deleteAccount(_ context.Context, _, subject string) error {
	if _, ok := m.roles[subject]; !ok {
		return platform.ErrNotFound
	}
	for _, teacher := range m.owners {
		if teacher == subject {
			return errOwnsClassrooms
		}
	}
	delete(m.roles, subject)
	m.deleted = append(m.deleted, subject)
	return nil
}

// call sends a request as a verified caller with the given roles.
func call(t *testing.T, store store, method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store}.routes, method, path, body, subject, roles...)
}

// step is one request of a scenario, by subject with the space-separated roles, and the answer it expects: its
// status and, when want is set, a part of the body.
type step struct {
	name, method, path, body, subject, roles string
	status                                   int
	want                                     string
}

// run sends the steps in order to the same store and stops at the first answer that is not the one expected.
func run(t *testing.T, store store, steps ...step) {
	t.Helper()
	for _, s := range steps {
		if w := call(t, store, s.method, s.path, s.body, s.subject, strings.Fields(s.roles)...); w.Code != s.status || !strings.Contains(w.Body.String(), s.want) {
			t.Fatalf("%s: %d %s", s.name, w.Code, w.Body.String())
		}
	}
}

func TestRegisterRecordsOneRole(t *testing.T) {
	store := newMemoryStore()
	run(t, store,
		step{"register", http.MethodPost, "/v1/me", "", "new-teacher", "teacher", http.StatusOK, ""},
		step{"a caller without a person role registered", http.MethodPost, "/v1/me", "", "service", "", http.StatusForbidden, ""},
		step{"an account with both roles registered", http.MethodPost, "/v1/me", "", "dual", "student teacher", http.StatusForbidden, "ROLE_CONFLICT"},
	)
	if len(store.registered) != 1 || store.registered[0] != "new-teacher:teacher::" {
		t.Fatalf("registered %v", store.registered)
	}
}

func TestARegisteredRoleNeverChanges(t *testing.T) {
	store := newMemoryStore()
	run(t, store,
		step{"a student signed in as a teacher", http.MethodPost, "/v1/me", "", "student-1", "teacher", http.StatusConflict, "ROLE_CONFLICT"},
		step{"a teacher signed in as a student", http.MethodPost, "/v1/me", "", "teacher-1", "student", http.StatusConflict, "ROLE_CONFLICT"},
	)
	if store.roles["student-1"] != "student" || store.roles["teacher-1"] != "teacher" || len(store.registered) != 0 {
		t.Fatalf("roles changed: %v %v", store.roles, store.registered)
	}
	run(t, store, step{"a student signing in again", http.MethodPost, "/v1/me", "", "student-1", "student", http.StatusOK, ""})
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
	named := func(name string) string { return `{"name":"` + name + `"}` }
	run(t, newMemoryStore(),
		step{"blank name", http.MethodPost, "/v1/classrooms", named("  "), "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"student created a classroom", http.MethodPost, "/v1/classrooms", named("Year 5"), "student-1", "student", http.StatusForbidden, ""},
		step{"create", http.MethodPost, "/v1/classrooms", named(" Year 5 "), "teacher-1", "teacher", http.StatusCreated, `"name":"Year 5"`},
		// The limit counts characters, not bytes.
		step{"the most characters", http.MethodPost, "/v1/classrooms", named(strings.Repeat("é", maxName)), "teacher-1", "teacher", http.StatusCreated, ""},
		step{"one character more", http.MethodPost, "/v1/classrooms", named(strings.Repeat("é", maxName+1)), "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
	)
}

func TestCreateTellsAMissingProfileFromAFailure(t *testing.T) {
	run(t, newMemoryStore(),
		step{"unregistered teacher", http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "teacher-9", "teacher", http.StatusConflict, "PROFILE_REQUIRED"},
		step{"database failure", http.MethodPost, "/v1/classrooms", `{"name":"db down"}`, "teacher-1", "teacher", http.StatusInternalServerError, ""},
	)
}

func TestTeachersCannotBeEnrolled(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	for _, body := range []string{`{"username":"lin"}`, `{"student_id":"teacher-2"}`, `{"student_id":"teacher-1"}`} {
		if w := call(t, store, http.MethodPost, path, body, "teacher-1", "teacher"); w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("enrolled a teacher with %s: %d %s", body, w.Code, w.Body.String())
		}
	}
	if len(store.enrolled[room]) != 1 {
		t.Fatalf("enrollments changed: %v", store.enrolled[room])
	}
}

func TestOnlyTheOwnerEnrollsKnownStudents(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room + "/enrollments"
	run(t, store,
		step{"another teacher enrolled", http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"unknown student", http.MethodPost, path, `{"student_id":"stranger"}`, "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"enroll", http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-1", "teacher", http.StatusOK, ""},
	)
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
	roster, ids := "/v1/classrooms/"+room+"/students", "/v1/classrooms/"+room+"/student-ids"
	remove := "/v1/classrooms/" + room + "/enrollments/student-1"
	run(t, store,
		step{"a student read the roster", http.MethodGet, roster, "", "student-1", "student", http.StatusForbidden, ""},
		step{"another teacher read the roster", http.MethodGet, roster, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"roster", http.MethodGet, roster, "", "teacher-1", "teacher", http.StatusOK, `"username":"ada"`},
		step{"a roster page", http.MethodGet, roster + "?offset=0&limit=500", "", "teacher-1", "teacher", http.StatusOK, `"total":1`},
		step{"past the roster's end", http.MethodGet, roster + "?offset=1", "", "teacher-1", "teacher", http.StatusOK, `{"students":[],"total":1}`},
		step{"a roster page too long", http.MethodGet, roster + "?limit=501", "", "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"a roster page before the start", http.MethodGet, roster + "?offset=-1", "", "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"a student read the roster's ids", http.MethodGet, ids, "", "student-1", "student", http.StatusForbidden, ""},
		step{"another teacher read the roster's ids", http.MethodGet, ids, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"roster ids", http.MethodGet, ids, "", "teacher-1", "teacher", http.StatusOK, `["student-1"]`},
		step{"another teacher removed a student", http.MethodDelete, remove, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"remove", http.MethodDelete, remove, "", "teacher-1", "teacher", http.StatusOK, ""},
		step{"removing twice", http.MethodDelete, remove, "", "teacher-1", "teacher", http.StatusNotFound, ""},
		step{"roster ids after the removal", http.MethodGet, ids, "", "teacher-1", "teacher", http.StatusOK, `[]`},
	)
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Fatal("student-1 is still enrolled")
	}
}

func TestOnlyTheOwnerDeletesAClassroom(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/classrooms/" + room
	run(t, store,
		step{"a student deleted the classroom", http.MethodDelete, path, "", "student-1", "student", http.StatusForbidden, ""},
		step{"another teacher deleted the classroom", http.MethodDelete, path, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"delete", http.MethodDelete, path, "", "teacher-1", "teacher", http.StatusOK, ""},
		// Once deleted, nobody has access, including its teacher and its students.
		step{"its teacher still has access", http.MethodGet, path + "/access", "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"its student still has access", http.MethodGet, path + "/access", "", "student-1", "student", http.StatusForbidden, ""},
	)
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Error("student-1 is still enrolled in the deleted classroom")
	}
	if store.roles["student-1"] != "student" {
		t.Error("deleting the classroom removed student-1's account")
	}
}

func TestOnlyTeachersListTheSchoolsStudents(t *testing.T) {
	store := newMemoryStore()
	run(t, store, step{"a student listed every student", http.MethodGet, "/v1/students", "", "student-1", "student", http.StatusForbidden, ""})
	cases := []struct {
		query, want string
		status      int
	}{
		{"", `"total":3`, http.StatusOK}, // the teacher named lin is not listed
		{"?search=GRACE", `"total":2`, http.StatusOK},
		{"?offset=2&limit=1", `"students":[{"id":"student-3"`, http.StatusOK},
		{"?offset=5", `"students":[],"total":3`, http.StatusOK},
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

func TestUsersDeleteOnlyTheirOwnAccount(t *testing.T) {
	store := newMemoryStore()
	student, teacher := "0b0e1f2a-3b4c-4d5e-8f60-718293a4b5c6", "1c1f2a3b-4c5d-4e6f-9071-8293a4b5c6d7"
	store.roles[student], store.roles[teacher] = "student", "teacher"
	store.owners[room] = teacher
	run(t, store,
		step{"a teacher with a classroom deleted their account", http.MethodDelete, "/v1/me", "", teacher, "teacher", http.StatusConflict, "OWNS_CLASSROOMS"},
		step{"delete", http.MethodDelete, "/v1/me", "", student, "student", http.StatusNoContent, ""},
		step{"delete again", http.MethodDelete, "/v1/me", "", student, "student", http.StatusNotFound, ""},
		step{"a malformed account id", http.MethodDelete, "/v1/me", "", "not-a-uuid", "student", http.StatusNotFound, ""},
	)
	if len(store.deleted) != 1 || store.deleted[0] != student || store.roles["student-1"] != "student" {
		t.Fatalf("deleted %v", store.deleted)
	}
}

func TestKeycloakRemovesASignInOnceAndTreatsAMissingOneAsRemoved(t *testing.T) {
	var calls []string
	keycloak := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		switch r.URL.Path {
		case "/realms/learning/protocol/openid-connect/token":
			_, _ = w.Write([]byte(`{"access_token":"admin","expires_in":60}`))
		case "/admin/realms/learning/users/gone":
			w.WriteHeader(http.StatusNotFound)
		case "/admin/realms/learning/users/broken":
			w.WriteHeader(http.StatusForbidden)
		default:
			if r.Header.Get("Authorization") != "Bearer admin" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer keycloak.Close()
	t.Setenv("KEYCLOAK_URL", keycloak.URL)
	t.Setenv("ACCOUNTS_CLIENT_SECRET", "secret")
	k := newKeycloakUsers()
	for user, ok := range map[string]bool{"u1": true, "gone": true, "broken": false} {
		if err := k.remove(context.Background(), user); (err == nil) != ok {
			t.Errorf("%s: %v", user, err)
		}
	}
	if calls[0] != "POST /realms/learning/protocol/openid-connect/token" {
		t.Fatalf("calls %v", calls)
	}
}

func (m *memoryStore) countClassrooms(ctx context.Context, org, subject string, teacher bool) (int, error) {
	items, err := m.listClassrooms(ctx, org, subject, teacher)
	return len(items), err
}
