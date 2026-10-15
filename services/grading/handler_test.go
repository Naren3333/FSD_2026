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
	room         = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
)

// memoryStore holds one draft evaluation; finalize fails with the error it is given.
type memoryStore struct {
	finalizeErr error
	finalized   []string // reasons
}

func (m *memoryStore) listEvaluations(context.Context, string, string) ([]Evaluation, error) {
	return []Evaluation{}, nil
}

// listFinalized answers for student-1 only, so a test sees whose results were asked for.
func (m *memoryStore) listFinalized(_ context.Context, _, _, student string) ([]Evaluation, error) {
	if student != "student-1" {
		return []Evaluation{}, nil
	}
	return []Evaluation{{ID: evaluationID, Student: student, Status: statusFinalized}}, nil
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

func (m *memoryStore) finalizeEvaluation(_ context.Context, _, id, _, reason string, _ map[string]int) (Evaluation, error) {
	if m.finalizeErr != nil {
		return Evaluation{}, m.finalizeErr
	}
	m.finalized = append(m.finalized, reason)
	return Evaluation{ID: id, Classroom: room, Status: "finalized"}, nil
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
		{"teacher enrolled here", platform.RelationStudent, evaluationID, `{"reason":"Checked"}`, nil, []string{"teacher", "student"}, http.StatusForbidden},
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
	if w.Code != http.StatusOK || w.Body.String() != `{"drafts":1,"finalized":0}`+"\n" {
		t.Fatalf("%d %q", w.Code, w.Body.String())
	}
}
