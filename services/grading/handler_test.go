package main

import (
	"context"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/jackc/pgx/v5"
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

func (m *memoryStore) findClassroom(_ context.Context, _, id string) (string, error) {
	if id != evaluationID {
		return "", pgx.ErrNoRows
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
		{"finalized", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, nil, []string{"teacher"}, 200},
		{"no reason", platform.RelationTeacher, evaluationID, `{"reason":" "}`, nil, []string{"teacher"}, 422},
		{"unknown evaluation", platform.RelationTeacher, missing, `{"reason":"Checked"}`, nil, []string{"teacher"}, 404},
		{"a student", platform.RelationStudent, evaluationID, `{"reason":"Checked"}`, nil, []string{"student"}, 403},
		{"teacher enrolled here", platform.RelationStudent, evaluationID, `{"reason":"Checked"}`, nil, []string{"teacher", "student"}, 403},
		{"already final", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errAlreadyFinalized, []string{"teacher"}, 409},
		{"score too high", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errScoreOutOfBounds, []string{"teacher"}, 422},
		{"unknown question", platform.RelationTeacher, evaluationID, `{"reason":"Checked"}`, errUnknownQuestion, []string{"teacher"}, 422},
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
