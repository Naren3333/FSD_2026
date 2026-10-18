package platform

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestStrictJSON(t *testing.T) {
	for _, body := range []string{`{"extra":1}`, `{} {}`, `null []`} {
		w := httptest.NewRecorder()
		r := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body))
		var value struct{}
		if Decode(w, r, &value) {
			t.Fatalf("accepted %s", body)
		}
	}
}
func TestOversizedJSONIsTooLarge(t *testing.T) {
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"name":"`+strings.Repeat("a", 3<<20)+`"}`))
	var value struct{ Name string }
	if Decode(w, r, &value) || w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status %d", w.Code)
	}
}

func TestPageReadsOffsetAndLimit(t *testing.T) {
	cases := []struct {
		query         string
		offset, limit int
		ok            bool
	}{
		{"", 0, 20, true},
		{"?offset=40&limit=100", 40, 100, true},
		{"?limit=0", 0, 0, false},
		{"?limit=101", 0, 0, false},
		{"?offset=-1", 0, 0, false},
		{"?offset=a", 0, 0, false},
	}
	for _, tc := range cases {
		w := httptest.NewRecorder()
		offset, limit, ok := Page(w, httptest.NewRequest(http.MethodGet, "/"+tc.query, nil), 20, 100)
		if offset != tc.offset || limit != tc.limit || ok != tc.ok || (!ok && w.Code != http.StatusUnprocessableEntity) {
			t.Errorf("%q: %d %d %v (%d)", tc.query, offset, limit, ok, w.Code)
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
func identityStub(t *testing.T, status int, body string) *Identity {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return NewIdentity(server.URL, server.Client())
}

func asTeacher(r *http.Request) *http.Request {
	var c Claims
	c.Subject, c.Org, c.Realm.Roles = "teacher-1", "org", []string{"teacher"}
	return r.WithContext(WithClaims(r.Context(), c))
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
		// Identity decides: a teacher-role token it answers "student" for gets no teacher powers.
		{200, `{"allowed":true,"relation":"student"}`, false, 403},
		{200, `{"allowed":true}`, false, 503},
		{403, `{}`, false, 403},
		{401, `{}`, false, 401},
		{500, `{}`, false, 503},
	}
	for _, tc := range cases {
		a := identityStub(t, tc.status, tc.body)
		w := httptest.NewRecorder()
		if got := TeacherOf(a, w, asTeacher(httptest.NewRequest(http.MethodGet, "/", nil)), classroom); got != tc.allow || (!got && w.Code != tc.code) {
			t.Errorf("%d %s: allowed %v, status %d", tc.status, tc.body, got, w.Code)
		}
	}
}

func TestRelationReportsStudent(t *testing.T) {
	a := identityStub(t, 200, `{"allowed":true,"relation":"student"}`)
	relation, ok := a.Relation(httptest.NewRecorder(), asTeacher(httptest.NewRequest(http.MethodGet, "/", nil)), classroom, "")
	if !ok || relation != RelationStudent {
		t.Fatalf("got %q %v", relation, ok)
	}
}

func TestRosterReadsTheEnrolledStudents(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   []string
		code   int
	}{
		{200, `["s-1","s-2"]`, []string{"s-1", "s-2"}, 200},
		{200, `[]`, []string{}, 200},
		{200, `["s-1",""]`, nil, 503},
		{200, `[{"id":"s-1"}]`, nil, 503},
		{200, `null`, nil, 503},
		{200, `not json`, nil, 503},
		{403, `{}`, nil, 403},
		{401, `{}`, nil, 401},
		{500, `{}`, nil, 503},
	}
	for _, tc := range cases {
		w := httptest.NewRecorder()
		got, ok := identityStub(t, tc.status, tc.body).Roster(w, asTeacher(httptest.NewRequest(http.MethodGet, "/", nil)), classroom)
		if ok != (tc.want != nil) || !slices.Equal(got, tc.want) || (!ok && w.Code != tc.code) {
			t.Errorf("%d %s: got %v %v, status %d", tc.status, tc.body, got, ok, w.Code)
		}
	}
}

// A roster over maxRoster is refused like an unreadable one, and the log says it was the size, for which classroom.
func TestAnOversizedRosterIsLoggedAsOverTheLimit(t *testing.T) {
	var logged bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logged, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	cases := []struct{ body, cause string }{
		{`["` + strings.Repeat("s", maxRoster) + `"]`, "over the size limit"},
		{`not json`, "unreadable"},
	}
	for _, tc := range cases {
		logged.Reset()
		w := httptest.NewRecorder()
		if _, ok := identityStub(t, 200, tc.body).Roster(w, asTeacher(httptest.NewRequest(http.MethodGet, "/", nil)), classroom); ok || w.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s: accepted or status %d", tc.cause, w.Code)
		}
		var entry struct{ Msg, Classroom, Cause string }
		if err := json.Unmarshal(logged.Bytes(), &entry); err != nil || entry.Msg != "classroom_roster_rejected" || entry.Classroom != classroom || entry.Cause != tc.cause {
			t.Errorf("%s: logged %s (%v)", tc.cause, logged.String(), err)
		}
	}
}

func TestRosterAsksForTheIDOnlyRoster(t *testing.T) {
	var asked string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		asked = r.URL.Path
		_, _ = w.Write([]byte(`["s-1"]`))
	}))
	t.Cleanup(server.Close)
	got, ok := NewIdentity(server.URL, server.Client()).Roster(httptest.NewRecorder(), asTeacher(httptest.NewRequest(http.MethodGet, "/", nil)), classroom)
	if !ok || !slices.Equal(got, []string{"s-1"}) || asked != "/v1/classrooms/"+classroom+"/student-ids" {
		t.Fatalf("asked %s: %v %v", asked, got, ok)
	}
}

func TestSlowRoutesGetTheirOwnDeadline(t *testing.T) {
	a := &App{Mux: http.NewServeMux(), auth: &Authenticator{}, slow: map[string]time.Duration{}} // /health/ routes skip auth
	a.Slow("GET /health/upload", 5*time.Minute)
	left := map[string]time.Duration{}
	for _, path := range []string{"/health/upload", "/health/other"} {
		a.Mux.HandleFunc("GET "+path, func(_ http.ResponseWriter, r *http.Request) {
			deadline, _ := r.Context().Deadline()
			left[path] = time.Until(deadline)
		})
		a.Handler().ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, path, nil))
	}
	if left["/health/upload"] < 4*time.Minute || left["/health/other"] > requestTimeout {
		t.Fatalf("deadlines %v", left)
	}
}

func TestHandlersCanExtendTheirOwnWriteDeadline(t *testing.T) {
	a := &App{Mux: http.NewServeMux(), auth: &Authenticator{}, slow: map[string]time.Duration{}}
	var err error
	a.Mux.HandleFunc("GET /health/download", func(w http.ResponseWriter, _ *http.Request) {
		err = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(time.Minute))
	})
	server := httptest.NewServer(a.Handler())
	defer server.Close()
	res, getErr := server.Client().Get(server.URL + "/health/download")
	if getErr != nil {
		t.Fatal(getErr)
	}
	res.Body.Close()
	if err != nil {
		t.Fatalf("SetWriteDeadline: %v", err)
	}
}

func TestFoundAnswersNotFoundOrFails(t *testing.T) {
	cases := []struct {
		err   error
		found bool
		code  int
	}{
		{nil, true, http.StatusOK},
		{ErrNotFound, false, http.StatusNotFound},
		{errors.Join(errors.New("wrapped"), ErrNotFound), false, http.StatusNotFound},
		{errors.New("db down"), false, http.StatusInternalServerError},
	}
	for _, tc := range cases {
		w := httptest.NewRecorder()
		if got := Found(w, tc.err, "Thing not found."); got != tc.found || w.Code != tc.code {
			t.Errorf("%v: found %v, status %d", tc.err, got, w.Code)
		}
	}
}

func TestForwardCarriesTheCallersTokenAndCorrelation(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/", nil)
	r.Header.Set("Authorization", "Bearer caller")
	r = r.WithContext(context.WithValue(r.Context(), correlationKey, "corr-1"))
	req, err := Forward(r, http.MethodGet, "http://curriculum/v1/topics/x")
	if err != nil || req.Header.Get("Authorization") != "Bearer caller" || req.Header.Get("X-Correlation-ID") != "corr-1" || req.Method != http.MethodGet {
		t.Fatalf("%v %v", err, req)
	}
}
