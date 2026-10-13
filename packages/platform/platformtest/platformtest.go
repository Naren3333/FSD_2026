// Package platformtest has test doubles for handler tests: a verified caller and a fixed classroom-access answer.
// Only test files import it.
package platformtest

import (
	"context"
	"learning/platform"
	"net/http"
	"net/http/httptest"
	"strings"
)

// Serve sends one request through the routes as a verified caller in organization "org" with the given roles.
func Serve(routes func(*http.ServeMux), method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	mux := http.NewServeMux()
	routes(mux)
	var c platform.Claims
	c.Subject, c.Org, c.Realm.Roles = subject, "org", roles
	r := httptest.NewRequestWithContext(platform.WithClaims(context.Background(), c), method, path, strings.NewReader(body))
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, r)
	return w
}

// Access answers every classroom check with one relation: platform.RelationTeacher, platform.RelationStudent,
// or "" to deny.
type Access string

// Relation implements platform.ClassroomAccess.
func (a Access) Relation(w http.ResponseWriter, _ *http.Request, _, _ string) (string, bool) {
	if a == "" {
		platform.Error(w, 403, "FORBIDDEN", "You do not have access to this classroom or student.")
		return "", false
	}
	return string(a), true
}

// Access implements platform.ClassroomAccess.
func (a Access) Access(w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	_, ok := a.Relation(w, r, classroom, student)
	return ok
}

// TeacherOf implements platform.ClassroomAccess, with the same role check as the real one.
func (a Access) TeacherOf(w http.ResponseWriter, r *http.Request, classroom string) bool {
	if !platform.Teacher(w, r) {
		return false
	}
	relation, ok := a.Relation(w, r, classroom, "")
	if ok && relation != platform.RelationTeacher {
		platform.Error(w, 403, "FORBIDDEN", "Only the classroom's teacher may do this.")
		return false
	}
	return ok
}
