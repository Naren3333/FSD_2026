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

// another spelling of the same uuid would hash to another advisory lock and miss text matches on events.
func TestIDTakesOnlyTheCanonicalSpelling(t *testing.T) {
	if !ID("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11") {
		t.Fatal("canonical uuid refused")
	}
	for _, other := range []string{"A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11", "{a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11}",
		"urn:uuid:a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "a0eebc999c0b4ef8bb6d6bb9bd380a11", ""} {
		if ID(other) {
			t.Errorf("%q accepted", other)
		}
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

// the answer's shape follows the route the mux matched, not the path's text.
func TestServeListPagesOnlyThePageRoute(t *testing.T) {
	mux := http.NewServeMux()
	serve := func(w http.ResponseWriter, r *http.Request) {
		ServeList(w, r, 100, func(paging ...ListWindow) ([]string, error) { return []string{r.PathValue("id")}, nil },
			func() (int, error) { return 7, nil })
	}
	mux.HandleFunc("GET /v1/things/{id}", serve)
	mux.HandleFunc("GET /v1/things/{id}/items/page", serve)
	for path, want := range map[string]string{
		"/v1/things/page":            `["page"]`,
		"/v1/things/a/items/page":    `{"items":["a"],"total":7}`,
		"/v1/things/a/items/page?x=": `{"items":["a"],"total":7}`,
	} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, path, nil))
		if got := strings.TrimSpace(w.Body.String()); w.Code != http.StatusOK || got != want {
			t.Errorf("%s: %d %s, want %s", path, w.Code, got, want)
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
		// identity decides: a teacher-role token it answers "student" for gets no teacher powers.
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

// a roster over maxRoster is refused like an unreadable one, and the log says it was the size, for which
// classroom.
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

type headerSequence struct {
	*httptest.ResponseRecorder
	codes []int
}

func (w *headerSequence) WriteHeader(status int) {
	w.codes = append(w.codes, status)
	if status >= 200 || status == http.StatusSwitchingProtocols {
		w.ResponseRecorder.WriteHeader(status)
	}
}

func TestInformationalHeadersDoNotStartFinalResponse(t *testing.T) {
	for _, final := range []int{http.StatusOK, http.StatusSwitchingProtocols} {
		underlying := &headerSequence{ResponseRecorder: httptest.NewRecorder()}
		observations := 0
		writer := &responseRecorder{ResponseWriter: underlying, onHeaders: func() { observations++ }}
		writer.WriteHeader(http.StatusContinue)
		writer.WriteHeader(http.StatusEarlyHints)
		if writer.status != 0 || observations != 0 {
			t.Fatalf("interim headers set status %d with %d observations", writer.status, observations)
		}
		if final == http.StatusOK {
			if _, err := writer.Write([]byte("body")); err != nil {
				t.Fatal(err)
			}
		} else {
			writer.WriteHeader(final)
		}
		want := []int{http.StatusContinue, http.StatusEarlyHints, final}
		if writer.status != final || observations != 1 || !slices.Equal(underlying.codes, want) {
			t.Fatalf("got status %d, %d observations and headers %v, want %v", writer.status, observations, underlying.codes, want)
		}
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

// a client's other spelling of an id in the path or query reaches the handler canonically; other text doesn't change.
func TestAnIDInThePathOrQueryReachesHandlersCanonically(t *testing.T) {
	a := &App{Mux: http.NewServeMux(), slow: map[string]time.Duration{}}
	a.Mux.HandleFunc("GET /health/things/{id}", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(r.PathValue("id") + " " + r.URL.Query().Get("topic_id") + " " + r.URL.Query().Get("hash")))
	})
	id, hash := "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "A0EEBC999C0B4EF8BB6D6BB9BD380A11"
	req := httptest.NewRequest(http.MethodGet, "/health/things/"+strings.ToUpper(id)+"?topic_id="+strings.ToUpper(id)+"&hash="+hash, nil)
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, req)
	if want := id + " " + id + " " + hash; w.Code != http.StatusOK || w.Body.String() != want {
		t.Fatalf("%d %q, want %q", w.Code, w.Body.String(), want)
	}
}

// another spelling of a caller's correlation id is the same trace, so it is respelled rather than replaced.
func TestACorrelationIDIsRespelledNotReplaced(t *testing.T) {
	a := &App{Mux: http.NewServeMux(), slow: map[string]time.Duration{}}
	a.Mux.HandleFunc("GET /health/live", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	id := "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
	req := httptest.NewRequest(http.MethodGet, "/health/live", nil)
	req.Header.Set("X-Correlation-ID", strings.ToUpper(id))
	w := httptest.NewRecorder()
	a.Handler().ServeHTTP(w, req)
	if got := w.Header().Get("X-Correlation-ID"); got != id {
		t.Fatalf("correlation %q, want %q", got, id)
	}
}
