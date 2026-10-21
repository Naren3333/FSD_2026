// package platformtest has test doubles for handler tests: a verified caller and a fixed classroom-access
// answer.
package platformtest

import (
	"context"
	"learning/platform"
	"net/http"
	"net/http/httptest"
	"strings"
)

// claims are verified claims for subject in organization "org" with the given realm roles.
func Claims(subject string, roles ...string) platform.Claims {
	c := platform.Claims{Subject: subject, Org: "org"}
	c.Realm.Roles = roles
	return c
}

// request is a request from a verified caller with claims c, for tests that also need to set headers.
func Request(c platform.Claims, method, path, body string) *http.Request {
	return httptest.NewRequestWithContext(platform.WithClaims(context.Background(), c), method, path, strings.NewReader(body))
}

// serverequest sends one request through the routes.
func ServeRequest(routes func(*http.ServeMux), r *http.Request) *httptest.ResponseRecorder {
	mux := http.NewServeMux()
	routes(mux)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, r)
	return w
}

// serve sends one request through the routes as a verified caller in organization "org" with the given
// roles.
func Serve(routes func(*http.ServeMux), method, path, body, subject string, roles ...string) *httptest.ResponseRecorder {
	return ServeRequest(routes, Request(Claims(subject, roles...), method, path, body))
}

// access answers every classroom check with one relation: platform.relationteacher,
// platform.relationstudent, or "" to deny. platform.access and platform.teacherof apply the real policy on
// top of it.
type Access string

// relation implements platform.classroomaccess.
func (a Access) Relation(w http.ResponseWriter, _ *http.Request, _, _ string) (string, bool) {
	if a == "" {
		platform.Forbidden(w, "You do not have access to this classroom or student.")
		return "", false
	}
	return string(a), true
}

// roster answers every roster request with the same enrolled students; nil denies it.
type Roster []string

// roster implements platform.classroomroster.
func (s Roster) Roster(w http.ResponseWriter, _ *http.Request, _ string) ([]string, bool) {
	if s == nil {
		platform.Forbidden(w, "Only the classroom's teacher may do this.")
		return nil, false
	}
	return s, true
}
