package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"strings"
	"testing"
	"time"
)

// racingStore is a memoryStore where the teacher's change lands between the handler's read of the assessment and the
// student's (or the other editor's) write: meanwhile runs first.
type racingStore struct {
	*memoryStore
	meanwhile func()
}

func (r racingStore) insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, bool, error) {
	r.meanwhile()
	return r.memoryStore.insertSubmission(ctx, v, sub)
}

func (r racingStore) saveAnswers(ctx context.Context, assessment, student string, answers map[string]string) (time.Time, error) {
	r.meanwhile()
	return r.memoryStore.saveAnswers(ctx, assessment, student, answers)
}

func (r racingStore) updateAssessment(ctx context.Context, org string, before, v Assessment) error {
	r.meanwhile()
	return r.memoryStore.updateAssessment(ctx, org, before, v)
}

// racing is a published one-question quiz whose store runs meanwhile before each racing write.
func racing(t *testing.T, meanwhile func(m *memoryStore, v Assessment)) (server, Assessment) {
	t.Helper()
	s, v := scheduled(t, "")
	m := s.store.(*memoryStore)
	s.store = racingStore{m, func() { meanwhile(m, v) }}
	return s, v
}

func deleteIt(m *memoryStore, v Assessment) { delete(m.assessments, v.ID) }

func changeIt(m *memoryStore, v Assessment) {
	v.Questions = append([]Question(nil), v.Questions...)
	v.Questions[0].Answer = "2"
	m.assessments[v.ID] = v
}

// pastDue moves the due date an hour back, with the given late policy, as a teacher's details change would.
func pastDue(policy string) func(*memoryStore, Assessment) {
	return func(m *memoryStore, v Assessment) {
		due := time.Now().Add(-time.Hour)
		v.DueAt, v.LatePolicy = &due, policy
		m.assessments[v.ID] = v
	}
}

func TestAStudentRacingTheTeachersChangeIsToldWhatHappened(t *testing.T) {
	cases := []struct {
		name      string
		meanwhile func(*memoryStore, Assessment)
		method    string
		path      string
		status    int
		code      string
	}{
		{"submit while it is deleted", deleteIt, http.MethodPost, "/submissions", http.StatusNotFound, "NOT_FOUND"},
		{"save while it is deleted", deleteIt, http.MethodPut, "/saved-answers", http.StatusNotFound, "NOT_FOUND"},
		{"submit while a question changes", changeIt, http.MethodPost, "/submissions", http.StatusConflict, "ASSESSMENT_CHANGED"},
		// The due date and late policy as stored when the submission commits decide, and the answer says what was stored.
		{"submit while late work becomes refused", pastDue(lateReject), http.MethodPost, "/submissions", http.StatusConflict, "CLOSED"},
		{"submit while the due date passes", pastDue(lateAccept), http.MethodPost, "/submissions", http.StatusCreated, `"late":true`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, v := racing(t, tc.meanwhile)
			body := `{"answers":{"` + v.Questions[0].ID + `":"1"}}`
			w := call(t, s, tc.method, "/v1/assessments/"+v.ID+tc.path, body, "student")
			if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.code) {
				t.Fatalf("%d %s", w.Code, w.Body.String())
			}
		})
	}
}

func TestAnEditRacingAnotherEditIsRefused(t *testing.T) {
	s, v := racing(t, changeIt)
	s.access = platformtest.Access(platform.RelationTeacher)
	renamed := strings.Replace(quiz, `"title":"Fractions"`, `"title":"Halves"`, 1)
	w := call(t, s, http.MethodPut, "/v1/assessments/"+v.ID, renamed, "teacher")
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "ASSESSMENT_CHANGED") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if got := s.store.(racingStore).assessments[v.ID]; got.Title != "Fractions" || got.Questions[0].Answer != "2" {
		t.Fatalf("the stale edit overwrote the other: %+v", got)
	}
}

func TestAPublishedAssessmentCannotGoBackToADraft(t *testing.T) {
	s, v := scheduled(t, "")
	s.access = platformtest.Access(platform.RelationTeacher)
	draft := strings.Replace(quiz, `"title":"Fractions",`, `"title":"Fractions","status":"draft",`, 1)
	if w := call(t, s, http.MethodPut, "/v1/assessments/"+v.ID, draft, "teacher"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "NOT_DRAFT") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if got := s.store.(*memoryStore).assessments[v.ID]; got.Status != statusPublished {
		t.Fatalf("now %s", got.Status)
	}
}

// patch sends a details change as the classroom's teacher and returns the stored assessment.
func patch(t *testing.T, s server, id, body string, want int) Assessment {
	t.Helper()
	if w := call(t, s, http.MethodPatch, "/v1/assessments/"+id, body, "teacher"); w.Code != want {
		t.Fatalf("%s: %d %s", body, w.Code, w.Body.String())
	}
	return s.store.(*memoryStore).assessments[id]
}

func TestDetailsChangesKeepToTheirRules(t *testing.T) {
	due := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	s, v := scheduled(t, `"due_at":`+stamp(due)+`,`)
	s.access = platformtest.Access(platform.RelationTeacher)

	if got := patch(t, s, v.ID, `{"due_at":"next week"}`, http.StatusUnprocessableEntity); got.DueAt == nil || !got.DueAt.Equal(due) {
		t.Fatalf("a malformed due date changed it: %+v", got.DueAt)
	}
	// Once it is past due, the title still changes, and so does the late policy, but no new past due date is set.
	s.clock = func() time.Time { return due.Add(time.Hour) }
	if got := patch(t, s, v.ID, `{"title":"Halves","late_policy":"reject"}`, http.StatusOK); got.Title != "Halves" || got.LatePolicy != lateReject || !got.DueAt.Equal(due) {
		t.Fatalf("renamed after its due date: %+v", got)
	}
	patch(t, s, v.ID, `{"due_at":`+stamp(due.Add(30*time.Minute))+`}`, http.StatusUnprocessableEntity)
	// null removes the due date.
	if got := patch(t, s, v.ID, `{"due_at":null}`, http.StatusOK); got.DueAt != nil || got.Title != "Halves" {
		t.Fatalf("null did not remove the due date: %+v", got)
	}

	patch(t, s, "2b1e4c1a-7c55-4c55-9a55-3f0c9f6a0d01", `{"title":"x"}`, http.StatusNotFound)
	s.access = platformtest.Access("") // a teacher, but not this classroom's
	if got := patch(t, s, v.ID, `{"title":"Taken"}`, http.StatusForbidden); got.Title != "Halves" {
		t.Fatalf("another classroom's teacher renamed it: %+v", got)
	}
}

func TestADraftsDetailsChangeToo(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, draftQuiz)
	past := time.Now().Add(-time.Hour)
	// A draft's due date is checked when it is published, not now.
	if got := patch(t, s, v.ID, `{"title":"Renamed","due_at":`+stamp(past)+`}`, http.StatusOK); got.Title != "Renamed" || got.Status != statusDraft {
		t.Fatalf("%+v", got)
	}
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/publish", "", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("published already past due: %d %s", w.Code, w.Body.String())
	}
}

func TestUnchangedQuestionsKeepTheirIDs(t *testing.T) {
	question := func(id, prompt string) Question {
		return Question{ID: id, Prompt: prompt, Type: typeNumerical, Answer: "1", Tolerance: "0", Points: 1, Topic: topic}
	}
	ids := func(qs []Question) string {
		out := []string{}
		for _, q := range qs {
			out = append(out, q.ID)
		}
		return strings.Join(out, ",")
	}
	cases := []struct {
		name          string
		before, after []Question
		want          string
	}{
		{"two identical questions keep one id each", []Question{question("a", "1?"), question("b", "1?")}, []Question{question("x", "1?"), question("y", "1?")}, "a,b"},
		{"reordered", []Question{question("a", "1?"), question("b", "2?")}, []Question{question("x", "2?"), question("y", "1?")}, "b,a"},
		{"a removed question's twin added", []Question{question("a", "1?"), question("b", "2?")}, []Question{question("x", "2?"), question("y", "3?"), question("z", "2?")}, "b,y,z"},
		{"changed", []Question{question("a", "1?")}, []Question{question("x", "1? ")}, "x"},
	}
	for _, tc := range cases {
		v := Assessment{Questions: tc.after}
		v.keepQuestionIDs(tc.before)
		if got := ids(v.Questions); got != tc.want {
			t.Errorf("%s: %s, want %s", tc.name, got, tc.want)
		}
	}
}

// The edit as the web app sends it: the stored questions with their ids, which are ignored.
func TestEditingWithTheQuestionsIDsSentKeepsThem(t *testing.T) {
	s, v := scheduled(t, "")
	s.access = platformtest.Access(platform.RelationTeacher)
	v.Title = "Renamed"
	body, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if got := put(t, s, v.ID, string(body)); got.Questions[0].ID != v.Questions[0].ID || got.Title != "Renamed" {
		t.Fatalf("%+v", got)
	}
}

func put(t *testing.T, s server, id, body string) Assessment {
	t.Helper()
	w := call(t, s, http.MethodPut, "/v1/assessments/"+id, body, "teacher")
	var got Assessment
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &got) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	return got
}
