package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
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

func (m *memoryStore) classroomAccess(ctx context.Context, classroom, student string) (string, string, bool, error) {
	org, teacher, err := m.findClassroomOwner(ctx, classroom)
	if err != nil {
		return "", "", false, err
	}
	enrolled, err := m.isEnrolled(ctx, classroom, student)
	return org, teacher, enrolled, err
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
	case m.roles[teacher] != "teacher":
		return errNotATeacher
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

func (m *memoryStore) insertAccount(_ context.Context, _, id, role, username, _ string) error {
	if username == "org.db-down" {
		return errors.New("connection refused")
	}
	m.roles[id], m.usernames[id] = role, username
	return nil
}

func (m *memoryStore) listAccounts(_ context.Context, _, role, search string, offset, limit int) ([]Account, int, error) {
	matching := []Account{}
	for id, r := range m.roles {
		_, local, prefixed := strings.Cut(m.usernames[id], ".") // as the store, past the school's id
		if !prefixed {
			local = m.usernames[id]
		}
		if r == role && strings.Contains(strings.ToLower(local), strings.ToLower(search)) {
			matching = append(matching, Account{ID: id, Username: m.usernames[id], Role: r})
		}
	}
	sort.Slice(matching, func(i, j int) bool { return matching[i].ID < matching[j].ID })
	end := min(offset+limit, len(matching))
	return matching[min(offset, end):end], len(matching), nil
}

func (m *memoryStore) findAccount(_ context.Context, _, id string) (Account, error) {
	role := m.roles[id]
	if role != "teacher" && role != "student" {
		return Account{}, platform.ErrNotFound
	}
	return Account{ID: id, Username: m.usernames[id], Role: role}, nil
}

func (m *memoryStore) listOrgClassrooms(context.Context, string, int, int) ([]SchoolClassroom, int, error) {
	result := []SchoolClassroom{}
	for id, teacher := range m.owners {
		result = append(result, SchoolClassroom{ID: id, Teacher: teacher, Students: len(m.enrolled[id])})
	}
	return result, len(result), nil
}

func (m *memoryStore) updateClassroom(_ context.Context, _, id string, _, teacher *string) error {
	if _, ok := m.owners[id]; !ok {
		return platform.ErrNotFound
	}
	if teacher != nil {
		if m.roles[*teacher] != "teacher" {
			return errNotATeacher
		}
		m.owners[id] = *teacher
	}
	return nil
}

// fakeAccounts stands in for keycloak's account creation.
type fakeAccounts struct {
	created, removed, reset []string
	fail                    error
}

func (f *fakeAccounts) resetPassword(_ context.Context, user string) (string, error) {
	if f.fail != nil {
		return "", f.fail
	}
	f.reset = append(f.reset, user)
	return "new-temporary-secret", nil
}

func (f *fakeAccounts) create(_ context.Context, org, role, username, _, _ string) (string, string, error) {
	if f.fail != nil {
		return "", "", f.fail
	}
	f.created = append(f.created, org+":"+role+":"+username)
	return "kc-" + username, "temporary-secret", nil
}

func (f *fakeAccounts) remove(_ context.Context, user string) error {
	f.removed = append(f.removed, user)
	return nil
}

// call sends a request as a verified caller with the given roles.
func call(t *testing.T, store store, method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(server{store: store, accounts: &fakeAccounts{}}.routes, method, path, body, subject, roles...)
}

// step is one request of a scenario, by subject with the space-separated roles, and the answer it expects:
// its status and, when want is set, a part of the body.
type step struct {
	name, method, path, body, subject, roles string
	status                                   int
	want                                     string
}

// run sends the steps in order to the same store and stops at the first answer that is not the one
// expected.
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

func TestRegisterRecordsTheNamesFromTheToken(t *testing.T) {
	store := newMemoryStore()
	c := platformtest.Claims("s9", "student")
	c.Username, c.Name = " lin ", "  "
	w := platformtest.ServeRequest(server{store: store}.routes, platformtest.Request(c, http.MethodPost, "/v1/me", ""))
	// without a full name, the username stands in for it.
	if w.Code != http.StatusOK || store.registered[0] != "s9:student:lin:lin" || !strings.Contains(w.Body.String(), `"username":"lin"`) {
		t.Fatalf("%d %v", w.Code, store.registered)
	}
}

// another is a classroom id no store has.
const another = "7f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"

func TestAdminsCreateClassroomsForTheirTeachers(t *testing.T) {
	body := func(name, teacher string) string { return `{"name":"` + name + `","teacher_id":"` + teacher + `"}` }
	path := "/v1/admin/classrooms"
	run(t, newMemoryStore(),
		step{"blank name", http.MethodPost, path, body("  ", "teacher-1"), "admin-1", "admin", http.StatusUnprocessableEntity, ""},
		step{"a teacher created a classroom", http.MethodPost, path, body("Year 5", "teacher-1"), "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"a student created a classroom", http.MethodPost, path, body("Year 5", "teacher-1"), "student-1", "student", http.StatusForbidden, ""},
		step{"the old teacher route", http.MethodPost, "/v1/classrooms", `{"name":"Year 5"}`, "teacher-1", "teacher", http.StatusMethodNotAllowed, ""},
		step{"create", http.MethodPost, path, body(" Year 5 ", "teacher-1"), "admin-1", "admin", http.StatusCreated, `"name":"Year 5","teacher_id":"teacher-1"`},
		step{"a student as its teacher", http.MethodPost, path, body("Year 5", "student-1"), "admin-1", "admin", http.StatusUnprocessableEntity, "INVALID_TEACHER"},
		step{"an unknown teacher", http.MethodPost, path, body("Year 5", "teacher-9"), "admin-1", "admin", http.StatusUnprocessableEntity, "INVALID_TEACHER"},
		// the limit counts characters, not bytes.
		step{"the most characters", http.MethodPost, path, body(strings.Repeat("é", maxName), "teacher-1"), "admin-1", "admin", http.StatusCreated, ""},
		step{"one character more", http.MethodPost, path, body(strings.Repeat("é", maxName+1), "teacher-1"), "admin-1", "admin", http.StatusUnprocessableEntity, ""},
		step{"database failure", http.MethodPost, path, body("db down", "teacher-1"), "admin-1", "admin", http.StatusInternalServerError, ""},
		step{"list", http.MethodGet, path, "", "admin-1", "admin", http.StatusOK, `"students":1`},
		step{"a teacher listed the school's classrooms", http.MethodGet, path, "", "teacher-1", "teacher", http.StatusForbidden, ""},
	)
}

func TestReassigningATeacherMovesTheirAccess(t *testing.T) {
	path, access := "/v1/admin/classrooms/"+room, "/v1/classrooms/"+room+"/access"
	run(t, newMemoryStore(),
		step{"a teacher reassigned their classroom", http.MethodPatch, path, `{"teacher_id":"teacher-2"}`, "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"nothing to change", http.MethodPatch, path, `{}`, "admin-1", "admin", http.StatusUnprocessableEntity, ""},
		step{"a student as teacher", http.MethodPatch, path, `{"teacher_id":"student-1"}`, "admin-1", "admin", http.StatusUnprocessableEntity, "INVALID_TEACHER"},
		step{"a blank name", http.MethodPatch, path, `{"name":" "}`, "admin-1", "admin", http.StatusUnprocessableEntity, ""},
		step{"an unknown classroom", http.MethodPatch, "/v1/admin/classrooms/" + another, `{"name":"x"}`, "admin-1", "admin", http.StatusNotFound, ""},
		step{"reassign", http.MethodPatch, path, `{"teacher_id":"teacher-2"}`, "admin-1", "admin", http.StatusOK, ""},
		step{"the old teacher's access", http.MethodGet, access, "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"the new teacher's access", http.MethodGet, access, "", "teacher-2", "teacher", http.StatusOK, `"relation":"teacher"`},
		// admins manage classrooms but have no relation to one, so no service shows them its data.
		step{"an admin's access", http.MethodGet, access, "", "admin-1", "admin", http.StatusForbidden, ""},
	)
}

func TestTeachersCannotBeEnrolled(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/admin/classrooms/" + room + "/enrollments"
	for _, body := range []string{`{"username":"lin"}`, `{"student_id":"teacher-2"}`, `{"student_id":"teacher-1"}`} {
		if w := call(t, store, http.MethodPost, path, body, "admin-1", "admin"); w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("enrolled a teacher with %s: %d %s", body, w.Code, w.Body.String())
		}
	}
	if len(store.enrolled[room]) != 1 {
		t.Fatalf("enrollments changed: %v", store.enrolled[room])
	}
}

func TestOnlyAdminsEnrollKnownStudents(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/admin/classrooms/" + room + "/enrollments"
	run(t, store,
		step{"its teacher enrolled", http.MethodPost, path, `{"student_id":"student-2"}`, "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"the old teacher route", http.MethodPost, "/v1/classrooms/" + room + "/enrollments", `{"student_id":"student-2"}`, "teacher-1", "teacher", http.StatusNotFound, ""},
		step{"unknown student", http.MethodPost, path, `{"student_id":"stranger"}`, "admin-1", "admin", http.StatusUnprocessableEntity, ""},
		step{"an unknown classroom", http.MethodPost, "/v1/admin/classrooms/" + another + "/enrollments", `{"student_id":"student-2"}`, "admin-1", "admin", http.StatusNotFound, ""},
		step{"enroll", http.MethodPost, path, `{"student_id":"student-2"}`, "admin-1", "admin", http.StatusOK, ""},
	)
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-2"); !enrolled {
		t.Fatal("student-2 was not enrolled")
	}
}

func TestAdminsOfAnotherSchoolSeeNoClassroom(t *testing.T) {
	store := newMemoryStore()
	other := platformtest.Claims("admin-9", "admin")
	other.Org = "other-school"
	for _, tc := range []struct{ method, path, body string }{
		{http.MethodGet, "/students", ""},
		{http.MethodGet, "/student-ids", ""},
		{http.MethodDelete, "", ""},
		{http.MethodPost, "/enrollments", `{"student_id":"student-2"}`},
		{http.MethodDelete, "/enrollments/student-1", ""},
	} {
		request := platformtest.Request(other, tc.method, "/v1/admin/classrooms/"+room+tc.path, tc.body)
		if w := platformtest.ServeRequest(server{store: store, accounts: &fakeAccounts{}}.routes, request); w.Code != http.StatusNotFound {
			t.Errorf("%s %s: %d %s", tc.method, tc.path, w.Code, w.Body.String())
		}
	}
	if store.owners[room] != "teacher-1" || len(store.enrolled[room]) != 1 {
		t.Fatalf("another school's admin changed the classroom: %v %v", store.owners, store.enrolled)
	}
}

func TestEnrollByUsername(t *testing.T) {
	store := newMemoryStore()
	path := "/v1/admin/classrooms/" + room + "/enrollments"
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
		if w := call(t, store, http.MethodPost, path, tc.body, "admin-1", "admin"); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.body, w.Code, w.Body.String())
		}
	}
}

func TestTeachersReadTheRosterAndAdminsRemoveStudents(t *testing.T) {
	store := newMemoryStore()
	roster, ids := "/v1/classrooms/"+room+"/students", "/v1/classrooms/"+room+"/student-ids"
	remove := "/v1/admin/classrooms/" + room + "/enrollments/student-1"
	run(t, store,
		step{"a student read the roster", http.MethodGet, roster, "", "student-1", "student", http.StatusForbidden, ""},
		step{"another teacher read the roster", http.MethodGet, roster, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"an admin read the teacher's roster", http.MethodGet, roster, "", "admin-1", "admin", http.StatusForbidden, ""},
		step{"roster", http.MethodGet, roster, "", "teacher-1", "teacher", http.StatusOK, `"username":"ada"`},
		step{"the admin's roster", http.MethodGet, "/v1/admin/classrooms/" + room + "/students", "", "admin-1", "admin", http.StatusOK, `"username":"ada"`},
		step{"a roster page", http.MethodGet, roster + "?offset=0&limit=500", "", "teacher-1", "teacher", http.StatusOK, `"total":1`},
		step{"past the roster's end", http.MethodGet, roster + "?offset=1", "", "teacher-1", "teacher", http.StatusOK, `{"students":[],"total":1}`},
		step{"a roster page too long", http.MethodGet, roster + "?limit=501", "", "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"a roster page before the start", http.MethodGet, roster + "?offset=-1", "", "teacher-1", "teacher", http.StatusUnprocessableEntity, ""},
		step{"a student read the roster's ids", http.MethodGet, ids, "", "student-1", "student", http.StatusForbidden, ""},
		step{"another teacher read the roster's ids", http.MethodGet, ids, "", "teacher-2", "teacher", http.StatusForbidden, ""},
		step{"roster ids", http.MethodGet, ids, "", "teacher-1", "teacher", http.StatusOK, `["student-1"]`},
		step{"the admin's roster ids", http.MethodGet, "/v1/admin/classrooms/" + room + "/student-ids", "", "admin-1", "admin", http.StatusOK, `["student-1"]`},
		step{"a teacher read the admin's roster ids", http.MethodGet, "/v1/admin/classrooms/" + room + "/student-ids", "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"its teacher removed a student", http.MethodDelete, remove, "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"remove", http.MethodDelete, remove, "", "admin-1", "admin", http.StatusOK, ""},
		step{"removing twice", http.MethodDelete, remove, "", "admin-1", "admin", http.StatusNotFound, ""},
		step{"roster ids after the removal", http.MethodGet, ids, "", "teacher-1", "teacher", http.StatusOK, `[]`},
	)
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Fatal("student-1 is still enrolled")
	}
}

func TestOnlyAdminsDeleteAClassroom(t *testing.T) {
	store := newMemoryStore()
	path, access := "/v1/admin/classrooms/"+room, "/v1/classrooms/"+room+"/access"
	run(t, store,
		step{"a student deleted the classroom", http.MethodDelete, path, "", "student-1", "student", http.StatusForbidden, ""},
		step{"its teacher deleted the classroom", http.MethodDelete, path, "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"the old teacher route", http.MethodDelete, "/v1/classrooms/" + room, "", "teacher-1", "teacher", http.StatusNotFound, ""},
		step{"delete", http.MethodDelete, path, "", "admin-1", "admin", http.StatusOK, ""},
		// once deleted, nobody has access, including its teacher and its students.
		step{"its teacher still has access", http.MethodGet, access, "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"its student still has access", http.MethodGet, access, "", "student-1", "student", http.StatusForbidden, ""},
		step{"deleting twice", http.MethodDelete, path, "", "admin-1", "admin", http.StatusNotFound, ""},
	)
	if enrolled, _ := store.isEnrolled(context.Background(), room, "student-1"); enrolled {
		t.Error("student-1 is still enrolled in the deleted classroom")
	}
	if store.roles["student-1"] != "student" {
		t.Error("deleting the classroom removed student-1's account")
	}
}

func TestOnlyAdminsListTheSchoolsAccounts(t *testing.T) {
	store := newMemoryStore()
	run(t, store,
		step{"a teacher listed every student", http.MethodGet, "/v1/admin/accounts?role=student", "", "teacher-1", "teacher", http.StatusForbidden, ""},
		step{"the old teacher route", http.MethodGet, "/v1/students", "", "teacher-1", "teacher", http.StatusNotFound, ""},
	)
	cases := []struct {
		query, want string
		status      int
	}{
		{"?role=student", `"total":3`, http.StatusOK},
		{"?role=teacher", `"total":2`, http.StatusOK},
		{"?role=admin", "", http.StatusUnprocessableEntity},
		{"", "", http.StatusUnprocessableEntity},
		{"?role=student&search=GRACE", `"total":2`, http.StatusOK},
		{"?role=student&offset=2&limit=1", `"accounts":[{"id":"student-3"`, http.StatusOK},
		{"?role=student&offset=5", `"accounts":[],"total":3`, http.StatusOK},
		{"?role=student&limit=0", "", http.StatusUnprocessableEntity},
		{"?role=student&limit=51", "", http.StatusUnprocessableEntity},
		{"?role=student&search=" + strings.Repeat("a", maxName+1), "", http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		w := call(t, store, http.MethodGet, "/v1/admin/accounts"+tc.query, "", "admin-1", "admin")
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.want) {
			t.Errorf("%s: %d %s", tc.query, w.Code, w.Body.String())
		}
	}
}

func TestAdminsCreateAccountsWithATemporaryPassword(t *testing.T) {
	store, keycloak := newMemoryStore(), &fakeAccounts{}
	serve := func(body, subject string, roles ...string) *httptest.ResponseRecorder {
		return platformtest.Serve(server{store: store, accounts: keycloak}.routes, http.MethodPost, "/v1/admin/accounts", body, subject, roles...)
	}
	account := func(role, username, first, last string) string {
		return `{"role":"` + role + `","username":"` + username + `","first_name":"` + first + `","last_name":"` + last + `"}`
	}
	for _, tc := range []struct {
		name, body string
		status     int
	}{
		{"an admin role", account("admin", "boss", "B", "Oss"), http.StatusUnprocessableEntity},
		{"a short username", account("student", "ab", "A", "B"), http.StatusUnprocessableEntity},
		{"a username with a space", account("student", "a b c", "A", "B"), http.StatusUnprocessableEntity},
		{"a username starting with a dot", account("student", ".abc", "A", "B"), http.StatusUnprocessableEntity},
		{"no last name", account("student", "abc", "A", " "), http.StatusUnprocessableEntity},
	} {
		if w := serve(tc.body, "admin-1", "admin"); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.name, w.Code, w.Body.String())
		}
	}
	if w := serve(account("student", "new.kid", "New", "Kid"), "teacher-1", "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("a teacher created an account: %d", w.Code)
	}
	w := serve(account("student", " New.Kid ", " New ", "Kid"), "admin-1", "admin")
	body := w.Body.String()
	if w.Code != http.StatusCreated || !strings.Contains(body, `"temporary_password":"temporary-secret"`) || !strings.Contains(body, `"username":"org.new.kid"`) ||
		!strings.Contains(body, `"display_name":"New Kid"`) || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("%d %s %v", w.Code, body, w.Header())
	}
	// the school's id starts the sign-in username, so another school's usernames are never in the way.
	if store.roles["kc-org.new.kid"] != "student" || store.usernames["kc-org.new.kid"] != "org.new.kid" || len(keycloak.created) != 1 || keycloak.created[0] != "org:student:org.new.kid" {
		t.Fatalf("created %v, roles %v", keycloak.created, store.roles)
	}
	// a failure to record the account removes the sign-in account keycloak made.
	if w := serve(account("teacher", "db-down", "D", "B"), "admin-1", "admin"); w.Code != http.StatusInternalServerError || len(keycloak.removed) != 1 || keycloak.removed[0] != "kc-org.db-down" {
		t.Fatalf("%d removed %v", w.Code, keycloak.removed)
	}
	keycloak.fail = errUsernameTaken
	if w := serve(account("student", "ada", "A", "Da"), "admin-1", "admin"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "USERNAME_TAKEN") {
		t.Fatalf("taken: %d %s", w.Code, w.Body.String())
	}
	keycloak.fail = errors.New("keycloak down")
	if w := serve(account("student", "zed", "Z", "Ed"), "admin-1", "admin"); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("keycloak down: %d %s", w.Code, w.Body.String())
	}
}

func TestAdminsResetASchoolAccountsPassword(t *testing.T) {
	store, keycloak := newMemoryStore(), &fakeAccounts{}
	serve := func(account, subject string, roles ...string) *httptest.ResponseRecorder {
		return platformtest.Serve(server{store: store, accounts: keycloak}.routes, http.MethodPost, "/v1/admin/accounts/"+account+"/password", "", subject, roles...)
	}
	if w := serve("student-1", "teacher-1", "teacher"); w.Code != http.StatusForbidden || len(keycloak.reset) != 0 {
		t.Fatalf("a teacher reset a password: %d", w.Code)
	}
	// an account that is not a teacher or student of the school, such as another admin, is not found.
	for _, account := range []string{"admin-2", "nobody"} {
		store.roles["admin-2"] = "admin"
		if w := serve(account, "admin-1", "admin"); w.Code != http.StatusNotFound || len(keycloak.reset) != 0 {
			t.Fatalf("%s: %d %s", account, w.Code, w.Body.String())
		}
	}
	w := serve("student-1", "admin-1", "admin")
	body := w.Body.String()
	if w.Code != http.StatusOK || !strings.Contains(body, `"temporary_password":"new-temporary-secret"`) || !strings.Contains(body, `"username":"ada"`) ||
		w.Header().Get("Cache-Control") != "no-store" || len(keycloak.reset) != 1 || keycloak.reset[0] != "student-1" {
		t.Fatalf("%d %s %v %v", w.Code, body, w.Header(), keycloak.reset)
	}
	keycloak.fail = errNoSignIn
	if w := serve("student-2", "admin-1", "admin"); w.Code != http.StatusNotFound {
		t.Fatalf("no sign-in account: %d", w.Code)
	}
	keycloak.fail = errors.New("keycloak down")
	if w := serve("student-2", "admin-1", "admin"); w.Code != http.StatusServiceUnavailable {
		t.Fatalf("keycloak down: %d", w.Code)
	}
}

func TestKeycloakResetsAPasswordToATemporaryOne(t *testing.T) {
	var sent map[string]any
	keycloak := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/realms/learning/protocol/openid-connect/token":
			_, _ = w.Write([]byte(`{"access_token":"admin","expires_in":60}`))
		case r.Method == http.MethodPut && r.URL.Path == "/admin/realms/learning/users/u1/reset-password":
			_ = json.NewDecoder(r.Body).Decode(&sent)
			w.WriteHeader(http.StatusNoContent)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer keycloak.Close()
	t.Setenv("KEYCLOAK_URL", keycloak.URL)
	t.Setenv("ACCOUNTS_CLIENT_SECRET", "secret")
	k := newKeycloakUsers()
	password, err := k.resetPassword(context.Background(), "u1")
	if err != nil || len(password) < 20 || sent["value"] != password || sent["temporary"] != true {
		t.Fatalf("%q %v %v", password, err, sent)
	}
	if _, err = k.resetPassword(context.Background(), "gone"); !errors.Is(err, errNoSignIn) {
		t.Fatalf("gone: %v", err)
	}
}

func TestAdminsRegisterAndCannotDeleteThemselves(t *testing.T) {
	run(t, newMemoryStore(),
		step{"register", http.MethodPost, "/v1/me", "", "admin-1", "admin", http.StatusOK, `"role":"admin"`},
		step{"delete", http.MethodDelete, "/v1/me", "", "2c1f2a3b-4c5d-4e6f-9071-8293a4b5c6d7", "admin", http.StatusForbidden, "ADMIN_ACCOUNT"},
	)
}

func TestKeycloakCreatesAnAccountInTheSchoolWithItsRole(t *testing.T) {
	var mu sync.Mutex
	var calls []string
	var created map[string]any
	keep, lateLooks := true, 0
	defer func(checks []time.Duration) { lostChecks = checks }(lostChecks)
	lostChecks = []time.Duration{0, 10 * time.Millisecond, 10 * time.Millisecond}
	keycloak := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		calls = append(calls, r.Method+" "+r.URL.Path)
		switch {
		case r.URL.Path == "/realms/learning/protocol/openid-connect/token":
			_, _ = w.Write([]byte(`{"access_token":"admin","expires_in":60}`))
		case r.Header.Get("Authorization") != "Bearer admin":
			w.WriteHeader(http.StatusUnauthorized)
		case r.Method == http.MethodPost && r.URL.Path == "/admin/realms/learning/users":
			_ = json.NewDecoder(r.Body).Decode(&created)
			if created["username"] == "taken" {
				w.WriteHeader(http.StatusConflict)
				return
			}
			if created["username"] == "lost" || created["username"] == "late" { // made, but answered after identity gave up
				mu.Unlock()
				time.Sleep(300 * time.Millisecond)
				mu.Lock()
			}
			if created["username"] == "gateway" { // made, but a proxy in front of keycloak answered for it
				w.WriteHeader(http.StatusBadGateway)
				return
			}
			w.Header().Set("Location", "http://keycloak/admin/realms/learning/users/new-id")
			w.WriteHeader(http.StatusCreated)
		case r.URL.Path == "/admin/realms/learning/roles/student":
			_, _ = w.Write([]byte(`{"id":"r1","name":"student"}`))
		case r.Method == http.MethodGet && r.URL.Path == "/admin/realms/learning/users":
			if r.URL.Query().Get("username") == "late" { // keycloak is still writing it at the first look
				if lateLooks++; lateLooks == 1 {
					_, _ = w.Write([]byte(`[]`))
					return
				}
			}
			// the account the last creation made, with the request's mark it was sent.
			attributes, _ := json.Marshal(created["attributes"])
			_, _ = w.Write([]byte(`[{"id":"lost-id","attributes":` + string(attributes) + `}]`))
		case r.Method == http.MethodGet && r.URL.Path == "/admin/realms/learning/users/new-id":
			org := `{"org_id":["school"]}`
			if !keep {
				org = `{}`
			}
			_, _ = w.Write([]byte(`{"id":"new-id","attributes":` + org + `}`))
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer keycloak.Close()
	t.Setenv("KEYCLOAK_URL", keycloak.URL)
	t.Setenv("ACCOUNTS_CLIENT_SECRET", "secret")
	k := newKeycloakUsers()
	id, password, err := k.create(context.Background(), "school", "student", "new.kid", "New", "Kid")
	if err != nil || id != "new-id" || len(password) < 20 {
		t.Fatalf("%q %d %v", id, len(password), err)
	}
	credentials, _ := created["credentials"].([]any)
	credential, _ := credentials[0].(map[string]any)
	if credential["value"] != password || credential["temporary"] != true || !strings.Contains(strings.Join(calls, ","), "POST /admin/realms/learning/users/new-id/role-mappings/realm") {
		t.Fatalf("created %v calls %v", created, calls)
	}
	if _, _, err = k.create(context.Background(), "school", "student", "taken", "T", "Aken"); !errors.Is(err, errUsernameTaken) {
		t.Fatalf("taken: %v", err)
	}
	// an admin who leaves midway does not stop the account being made.
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	if id, _, err = k.create(cancelled, "school", "student", "new.kid", "New", "Kid"); err != nil || id != "new-id" {
		t.Fatalf("cancelled: %q %v", id, err)
	}
	// a realm that drops the organization attribute gets no account at all.
	keep, calls = false, nil
	if _, _, err = k.create(context.Background(), "school", "student", "new.kid", "New", "Kid"); err == nil || calls[len(calls)-1] != "DELETE /admin/realms/learning/users/new-id" {
		t.Fatalf("%v %v", err, calls)
	}
	// an account keycloak made but whose answer was lost is removed; another request's with the username is not.
	keep, calls = true, nil
	k.client = &http.Client{Timeout: 100 * time.Millisecond}
	if _, _, err = k.create(context.Background(), "school", "student", "lost", "L", "Ost"); err == nil {
		t.Fatal("a lost answer was taken for success")
	}
	mu.Lock()
	lost := strings.Join(calls, ",")
	mu.Unlock()
	if !strings.Contains(lost, "DELETE /admin/realms/learning/users/lost-id") {
		t.Fatalf("lost: %v", lost)
	}
	removedLost := k.removeLost(context.Background(), "school", "lost", "another-request")
	mu.Lock()
	if removedLost != nil || strings.Count(strings.Join(calls, ","), "DELETE /admin/realms/learning/users/lost-id") != 1 {
		t.Fatalf("another request's account was removed: %v %v", removedLost, calls)
	}
	calls = nil
	mu.Unlock()
	// an account a proxy answered a server error for may exist, so it is removed too.
	k.client = &http.Client{}
	if _, _, err = k.create(context.Background(), "school", "student", "gateway", "G", "Ate"); err == nil {
		t.Fatal("a proxy's error was taken for success")
	}
	mu.Lock()
	if !strings.Contains(strings.Join(calls, ","), "DELETE /admin/realms/learning/users/lost-id") {
		t.Fatalf("gateway: %v", calls)
	}
	calls = nil
	mu.Unlock()
	k.client = &http.Client{Timeout: 100 * time.Millisecond}
	// an account keycloak finishes writing after the first look is found by a later one.
	if _, _, err = k.create(context.Background(), "school", "student", "late", "L", "Ate"); err == nil {
		t.Fatal("a lost answer was taken for success")
	}
	mu.Lock()
	defer mu.Unlock()
	if lateLooks != 2 || !strings.Contains(strings.Join(calls, ","), "DELETE /admin/realms/learning/users/lost-id") {
		t.Fatalf("late: %d looks, %v", lateLooks, calls)
	}
	calls = nil
	mu.Unlock()
	// an answer lost because the work ran out of time still has time to remove the account.
	defer func(work time.Duration) { accountWork = work }(accountWork)
	accountWork, k.client = 150*time.Millisecond, &http.Client{}
	if _, _, err = k.create(context.Background(), "school", "student", "lost", "L", "Ost"); err == nil {
		t.Fatal("a lost answer was taken for success")
	}
	mu.Lock()
	if !strings.Contains(strings.Join(calls, ","), "DELETE /admin/realms/learning/users/lost-id") {
		t.Fatalf("timed out: %v", calls)
	}
}

func TestARequestThatNeverReachedKeycloakLooksForNoAccount(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	closed.Close()
	t.Setenv("KEYCLOAK_URL", closed.URL)
	t.Setenv("ACCOUNTS_CLIENT_SECRET", "secret")
	defer func(checks []time.Duration) { lostChecks = checks }(lostChecks)
	lostChecks = []time.Duration{time.Hour} // a look would outlast the test
	started := time.Now()
	if _, _, err := newKeycloakUsers().create(context.Background(), "school", "student", "zed", "Z", "Ed"); err == nil || time.Since(started) > 10*time.Second {
		t.Fatalf("%v after %v", err, time.Since(started))
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

func TestSignInNamesStartWithTheSchool(t *testing.T) {
	if name, ok := signInName("local-school", "ada.l"); !ok || name != "local-school.ada.l" {
		t.Fatalf("%q %v", name, ok)
	}
	// a school id the first dot could not end, or no username could start with, is refused.
	for _, org := range []string{"", "a.b", "Upper", "a_b", "a@b", strings.Repeat("a", 65)} {
		if name, ok := signInName(org, "ada"); ok {
			t.Errorf("%q made %q", org, name)
		}
	}
}
