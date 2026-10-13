package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

const (
	room  = "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"
	skill = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
)

type memoryStore struct {
	assessments map[string]Assessment
	submitted   map[string]bool // assessment/student
}

func (m *memoryStore) findAssessment(_ context.Context, _, id string) (Assessment, error) {
	v, ok := m.assessments[id]
	if !ok {
		return v, pgx.ErrNoRows
	}
	return v, nil
}

func (m *memoryStore) insertAssessment(_ context.Context, _ string, v Assessment) error {
	m.assessments[v.ID] = v
	return nil
}

func (m *memoryStore) listAssessments(context.Context, string, string) ([]Summary, error) {
	return nil, nil
}

func (m *memoryStore) insertSubmission(_ context.Context, v Assessment, sub Submission) (bool, error) {
	key := v.ID + "/" + sub.Student
	if m.submitted[key] {
		return false, nil
	}
	m.submitted[key] = true
	return true, nil
}

type fixedSkills skillStatus

func (s fixedSkills) check(*http.Request, string) skillStatus { return skillStatus(s) }

func newServer(relation string, skillAnswer skillStatus) (server, *memoryStore) {
	store := &memoryStore{assessments: map[string]Assessment{}, submitted: map[string]bool{}}
	return server{store: store, access: platformtest.Access(relation), skills: fixedSkills(skillAnswer)}, store
}

func call(t *testing.T, s server, method, path, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(s.routes, method, path, body, "user", roles...)
}

const quiz = `{"classroom_id":"` + room + `","title":"Fractions","questions":[{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1","tolerance":"0","skill_id":"` + skill + `"}]}`

func TestCreateChecksTheTeacherAndEverySkill(t *testing.T) {
	cases := []struct {
		relation string
		skill    skillStatus
		status   int
	}{
		{platform.RelationTeacher, skillValid, 201},
		{platform.RelationStudent, skillValid, 403},
		{platform.RelationTeacher, skillInvalid, 422},
		{platform.RelationTeacher, skillUnavailable, 503},
	}
	for _, tc := range cases {
		s, _ := newServer(tc.relation, tc.skill)
		if w := call(t, s, http.MethodPost, "/v1/assessments", quiz, "teacher"); w.Code != tc.status {
			t.Errorf("%s/%d: %d %s", tc.relation, tc.skill, w.Code, w.Body.String())
		}
	}
}

func created(t *testing.T, relation string) (server, Assessment) {
	t.Helper()
	s, _ := newServer(platform.RelationTeacher, skillValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", quiz, "teacher")
	var v Assessment
	if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
		t.Fatal(err)
	}
	s.access = platformtest.Access(relation)
	return s, v
}

func TestStudentsSeeNoAnswerKey(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	w := call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "student")
	if w.Code != 200 || strings.Contains(w.Body.String(), `"answer"`) {
		t.Fatalf("student view: %d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access(platform.RelationTeacher)
	if w = call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "teacher"); !strings.Contains(w.Body.String(), `"answer":"1"`) {
		t.Fatalf("teacher view lacks the key: %s", w.Body.String())
	}
}

func TestEachStudentSubmitsOnce(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	path := "/v1/assessments/" + v.ID + "/submissions"
	body := `{"answers":{"` + v.Questions[0].ID + `":"1"}}`
	if w := call(t, s, http.MethodPost, path, body, "student"); w.Code != 201 {
		t.Fatalf("status %d %s", w.Code, w.Body.String())
	}
	if w := call(t, s, http.MethodPost, path, body, "student"); w.Code != 409 {
		t.Fatalf("second submission: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, path, `{"answers":{}}`, "student"); w.Code != 422 && w.Code != 409 {
		t.Fatalf("missing answers: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, path, body, "teacher", "student"); w.Code != 403 {
		t.Fatalf("a teacher submitted: %d", w.Code)
	}
}
