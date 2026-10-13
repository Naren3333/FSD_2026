package platform

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStrictJSON(t *testing.T) {
	for _, body := range []string{`{"extra":1}`, `{} {}`, `null []`} {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", "/", strings.NewReader(body))
		var value struct{}
		if Decode(w, r, &value) {
			t.Fatalf("accepted %s", body)
		}
	}
}
func TestRoles(t *testing.T) {
	var c Claims
	c.Realm.Roles = []string{"student"}
	if c.Has("teacher") || !c.Has("student") {
		t.Fatal("incorrect role authorization")
	}
}

// identityStub answers every access check with the given status and body.
func identityStub(t *testing.T, status int, body string) *App {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	t.Setenv("IDENTITY_URL", server.URL)
	return &App{Client: server.Client()}
}

func asTeacher(r *http.Request) *http.Request {
	var c Claims
	c.Subject, c.Org, c.Realm.Roles = "dual", "org", []string{"teacher", "student"}
	return r.WithContext(context.WithValue(r.Context(), claimsKey, c))
}

const classroom = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"

func TestTeacherOfNeedsTheTeacherRelation(t *testing.T) {
	cases := []struct {
		status int
		body   string
		allow  bool
		code   int
	}{
		{200, `{"allowed":true,"relation":"teacher"}`, true, 200},
		// A teacher-role account enrolled as a student: Identity says student, so no teacher powers.
		{200, `{"allowed":true,"relation":"student"}`, false, 403},
		{200, `{"allowed":true}`, false, 503},
		{403, `{}`, false, 403},
		{500, `{}`, false, 503},
	}
	for _, tc := range cases {
		a := identityStub(t, tc.status, tc.body)
		w := httptest.NewRecorder()
		if got := a.TeacherOf(w, asTeacher(httptest.NewRequest("GET", "/", nil)), classroom); got != tc.allow || (!got && w.Code != tc.code) {
			t.Errorf("%d %s: allowed %v, status %d", tc.status, tc.body, got, w.Code)
		}
	}
}

func TestRelationReportsStudent(t *testing.T) {
	a := identityStub(t, 200, `{"allowed":true,"relation":"student"}`)
	relation, ok := a.Relation(httptest.NewRecorder(), asTeacher(httptest.NewRequest("GET", "/", nil)), classroom, "")
	if !ok || relation != RelationStudent {
		t.Fatalf("got %q %v", relation, ok)
	}
}
