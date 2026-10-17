package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const (
	evaluationID = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"
	assessmentID = "9c4f8051-bd7e-4f60-8b5c-4d5e6f708192"
	room         = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
)

// memoryStore holds one draft evaluation; finalize fails with the error it is given.
type memoryStore struct {
	finalizeErr error
	finalized   []string // reasons
	marks       map[string]Mark
	released    []string // assessments
}

func (m *memoryStore) listEvaluations(_ context.Context, _, _, assessment string) ([]Evaluation, error) {
	if assessment != "" && assessment != assessmentID {
		return []Evaluation{}, nil
	}
	return []Evaluation{{ID: evaluationID, Assessment: assessmentID, Answers: map[string]string{"q1": "an answer"}, Evidence: []Evidence{{Question: "q1", Suggestion: &Suggestion{Status: suggestionReady, Feedback: "AI words"}}}}}, nil
}

// listReleased answers for student-1 only, so a test sees whose results were asked for. The evidence carries what
// students must never see: a suggestion and rubric criteria.
func (m *memoryStore) listReleased(_ context.Context, _, _, student string) ([]Evaluation, error) {
	if student != "student-1" {
		return []Evaluation{}, nil
	}
	evidence := []Evidence{{Question: "q1", Awarded: 2, Possible: 3, Feedback: "Good start.", Criteria: []CriterionScore{{"c1", 2}}, Suggestion: &Suggestion{Status: suggestionReady, Feedback: "AI words"}}}
	return []Evaluation{{ID: evaluationID, Student: student, Status: statusFinalized, Evidence: evidence}}, nil
}

func (m *memoryStore) countEvaluations(context.Context, string, string) (Counts, error) {
	return Counts{Drafts: 1 - len(m.finalized), Finalized: len(m.finalized)}, nil
}

func (m *memoryStore) findClassroom(_ context.Context, _, id string) (string, error) {
	if id != evaluationID {
		return "", platform.ErrNotFound
	}
	return room, nil
}

func (m *memoryStore) assessmentClassroom(_ context.Context, _, assessment string) (string, error) {
	if assessment != assessmentID {
		return "", platform.ErrNotFound
	}
	return room, nil
}

func (m *memoryStore) saveMarks(_ context.Context, _, id, _ string, marks map[string]Mark) (Evaluation, error) {
	if m.finalizeErr != nil {
		return Evaluation{}, m.finalizeErr
	}
	m.marks = marks
	return Evaluation{ID: id, Classroom: room, Status: statusDraft}, nil
}

func (m *memoryStore) finalizeEvaluation(_ context.Context, _, id, _, reason string, marks map[string]Mark) (Evaluation, error) {
	if m.finalizeErr != nil {
		return Evaluation{}, m.finalizeErr
	}
	m.finalized = append(m.finalized, reason)
	m.marks = marks
	return Evaluation{ID: id, Classroom: room, Status: "finalized"}, nil
}

func (m *memoryStore) release(_ context.Context, _, assessment, _ string) (int, error) {
	m.released = append(m.released, assessment)
	return 3, nil
}

func finalize(t *testing.T, store *memoryStore, relation, id, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	s := server{store: store, access: platformtest.Access(relation)}
	return platformtest.Serve(s.routes, http.MethodPost, "/v1/evaluations/"+id+"/finalize", body, "teacher-1", roles...)
}

func TestFinalizeMapsEachOutcome(t *testing.T) {
	missing := "8b3e7f40-ac6d-4e5f-9a4b-3c4d5e6f7081"
	cases := []struct {
		name, relation, id, body string
		err                      error
		roles                    []string
		status                   int
	}{
		{"finalized", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, nil, []string{"teacher"}, http.StatusOK},
		{"no reason", platform.RelationTeacher, evaluationID, `{"reason":" "}`, nil, []string{"teacher"}, http.StatusUnprocessableEntity},
		{"unknown evaluation", platform.RelationTeacher, missing, `{"reason":"Checked"}`, nil, []string{"teacher"}, http.StatusNotFound},
		{"a student", platform.RelationStudent, evaluationID, `{"reason":"Checked"}`, nil, []string{"student"}, http.StatusForbidden},
		{"teacher token, student relation", platform.RelationStudent, evaluationID, `{"reason":"Checked"}`, nil, []string{"teacher"}, http.StatusForbidden},
		{"already final", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errAlreadyFinalized, []string{"teacher"}, http.StatusConflict},
		{"score too high", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errScoreOutOfBounds, []string{"teacher"}, http.StatusUnprocessableEntity},
		{"unknown question", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errUnknownQuestion, []string{"teacher"}, http.StatusUnprocessableEntity},
	}
	for _, tc := range cases {
		store := &memoryStore{finalizeErr: tc.err}
		if w := finalize(t, store, tc.relation, tc.id, tc.body, tc.roles...); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.name, w.Code, w.Body.String())
		}
	}
}

func TestOnlyTheClassroomTeacherListsEvaluations(t *testing.T) {
	for relation, status := range map[string]int{platform.RelationTeacher: 200, platform.RelationStudent: 403} {
		s := server{store: &memoryStore{}, access: platformtest.Access(relation)}
		w := platformtest.Serve(s.routes, http.MethodGet, "/v1/evaluations?classroom_id="+room, "", "teacher-1", "teacher")
		if w.Code != status {
			t.Errorf("%s: %d", relation, w.Code)
		}
	}
}

func TestOnlyAnEnrolledStudentReadsTheirOwnResults(t *testing.T) {
	cases := []struct {
		relation, subject string
		roles             []string
		status            int
		body              string
	}{
		{platform.RelationStudent, "student-1", []string{"student"}, 200, evaluationID},
		{platform.RelationTeacher, "teacher-1", []string{"teacher"}, 403, ""},
		{"", "student-2", []string{"student"}, 403, ""},
	}
	for _, tc := range cases {
		s := server{store: &memoryStore{}, access: platformtest.Access(tc.relation)}
		w := platformtest.Serve(s.routes, http.MethodGet, "/v1/evaluations/mine?classroom_id="+room, "", tc.subject, tc.roles...)
		if w.Code != tc.status || !strings.Contains(w.Body.String(), tc.body) {
			t.Errorf("%s: %d %s", tc.subject, w.Code, w.Body.String())
		}
	}
}

// The decoder is tested in platform; this checks purge refuses before touching the database (tx is nil).
func TestPurgeRejectsMalformedDeletions(t *testing.T) {
	event := platform.Event{Resource: room, Data: json.RawMessage(`{"classroom_id":"` + room + `","org_id":""}`)}
	var invalid platform.InvalidEvent
	if !errors.As(purge(context.Background(), nil, event), &invalid) {
		t.Fatal("a malformed deletion was not rejected")
	}
}

func TestOnlyTheTeacherSeesGradingCounts(t *testing.T) {
	path := "/v1/evaluations/counts?classroom_id=" + room
	student := server{store: &memoryStore{}, access: platformtest.Access(platform.RelationStudent)}
	if w := platformtest.Serve(student.routes, http.MethodGet, path, "", "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student saw grading counts: %d", w.Code)
	}
	teacher := server{store: &memoryStore{}, access: platformtest.Access(platform.RelationTeacher)}
	w := platformtest.Serve(teacher.routes, http.MethodGet, path, "", "teacher-1", "teacher")
	if w.Code != http.StatusOK || w.Body.String() != `{"drafts":1,"finalized":0,"unreleased":0}`+"\n" {
		t.Fatalf("%d %q", w.Code, w.Body.String())
	}
}

func TestStudentsNeverSeeSuggestionsOrRubricMarks(t *testing.T) {
	s := server{store: &memoryStore{}, access: platformtest.Access(platform.RelationStudent)}
	w := platformtest.Serve(s.routes, http.MethodGet, "/v1/evaluations/mine?classroom_id="+room, "", "student-1", "student")
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "Good start.") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	for _, secret := range []string{"suggestion", "AI words", "criteria"} {
		if strings.Contains(w.Body.String(), secret) {
			t.Errorf("a student saw %q: %s", secret, w.Body.String())
		}
	}
}

func TestTheTeacherListsOneAssessmentWithAnswers(t *testing.T) {
	s := server{store: &memoryStore{}, access: platformtest.Access(platform.RelationTeacher)}
	w := platformtest.Serve(s.routes, http.MethodGet, "/v1/evaluations?classroom_id="+room+"&assessment_id="+assessmentID, "", "teacher-1", "teacher")
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), "an answer") || !strings.Contains(w.Body.String(), "AI words") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w = platformtest.Serve(s.routes, http.MethodGet, "/v1/evaluations?classroom_id="+room+"&assessment_id=nope", "", "teacher-1", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("a malformed assessment id: %d", w.Code)
	}
}

func TestOnlyTheClassroomTeacherSavesMarks(t *testing.T) {
	body := `{"marks":{"q2":{"criteria":{"c1":2},"feedback":"Good."}}}`
	cases := []struct {
		name, relation, body string
		err                  error
		roles                []string
		status               int
	}{
		{"saved", platform.RelationTeacher, body, nil, []string{"teacher"}, http.StatusOK},
		{"nothing to save", platform.RelationTeacher, `{"marks":{}}`, nil, []string{"teacher"}, http.StatusUnprocessableEntity},
		{"a student", platform.RelationStudent, body, nil, []string{"student"}, http.StatusForbidden},
		{"another teacher", platform.RelationStudent, body, nil, []string{"teacher"}, http.StatusForbidden},
		{"bad criteria", platform.RelationTeacher, body, errInvalidCriteria, []string{"teacher"}, http.StatusUnprocessableEntity},
		{"already final", platform.RelationTeacher, body, errAlreadyFinalized, []string{"teacher"}, http.StatusConflict},
	}
	for _, tc := range cases {
		store := &memoryStore{finalizeErr: tc.err}
		s := server{store: store, access: platformtest.Access(tc.relation)}
		w := platformtest.Serve(s.routes, http.MethodPost, "/v1/evaluations/"+evaluationID+"/marks", tc.body, "teacher-1", tc.roles...)
		if w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.name, w.Code, w.Body.String())
		}
		if tc.status == http.StatusOK && store.marks["q2"].Criteria["c1"] != 2 {
			t.Errorf("%s: marks not passed on: %+v", tc.name, store.marks)
		}
	}
}

func TestFinalizingNeedsEveryQuestionMarked(t *testing.T) {
	store := &memoryStore{finalizeErr: errUnmarked}
	w := finalize(t, store, platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, "teacher")
	if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "NEEDS_MARKING") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	store = &memoryStore{}
	w = finalize(t, store, platform.RelationTeacher, evaluationID, `{"reason":"Checked","overrides":{"q1":1},"marks":{"q2":{"criteria":{"c1":0}}}}`, "teacher")
	if w.Code != http.StatusOK || *store.marks["q1"].Awarded != 1 || store.marks["q2"].Criteria["c1"] != 0 {
		t.Fatalf("%d %+v", w.Code, store.marks)
	}
}

func TestOnlyTheClassroomTeacherReleasesResults(t *testing.T) {
	body := `{"assessment_id":"` + assessmentID + `"}`
	for relation, status := range map[string]int{platform.RelationTeacher: 200, platform.RelationStudent: 403} {
		store := &memoryStore{}
		s := server{store: store, access: platformtest.Access(relation)}
		w := platformtest.Serve(s.routes, http.MethodPost, "/v1/evaluations/release", body, "teacher-1", "teacher")
		if w.Code != status {
			t.Errorf("%s: %d", relation, w.Code)
		}
		if status == 200 && (len(store.released) != 1 || !strings.Contains(w.Body.String(), `"released":3`)) {
			t.Errorf("%s: %v %s", relation, store.released, w.Body.String())
		}
	}
	s := server{store: &memoryStore{}, access: platformtest.Access(platform.RelationTeacher)}
	unknown := `{"assessment_id":"` + room + `"}`
	if w := platformtest.Serve(s.routes, http.MethodPost, "/v1/evaluations/release", unknown, "teacher-1", "teacher"); w.Code != http.StatusNotFound {
		t.Fatalf("an assessment without grades: %d", w.Code)
	}
	if w := platformtest.Serve(s.routes, http.MethodPost, "/v1/evaluations/release", body, "student-1", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student released results: %d", w.Code)
	}
}

func TestAFinalizeReasonIsAtMost1000Characters(t *testing.T) {
	store := &memoryStore{}
	if w := finalize(t, store, platform.RelationTeacher, evaluationID, `{"reason":"`+strings.Repeat("a", 1001)+`"}`, "teacher"); w.Code != http.StatusUnprocessableEntity || len(store.finalized) != 0 {
		t.Fatalf("%d %v", w.Code, store.finalized)
	}
	if w := finalize(t, store, platform.RelationTeacher, evaluationID, `{"reason":"`+strings.Repeat("語", 1000)+`"}`, "teacher"); w.Code != http.StatusOK {
		t.Fatalf("1000 characters (3000 bytes) refused: %d", w.Code)
	}
}
