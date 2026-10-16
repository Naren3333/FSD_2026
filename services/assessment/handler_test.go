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
	"time"
)

const (
	room  = "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"
	skill = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
)

type memoryStore struct {
	assessments map[string]Assessment
	submitted   map[string]Submission // by assessment/student
	saved       map[string]map[string]string
}

func (m *memoryStore) findAssessment(_ context.Context, _, id string) (Assessment, error) {
	v, ok := m.assessments[id]
	if !ok {
		return v, platform.ErrNotFound
	}
	return v, nil
}

func (m *memoryStore) insertAssessment(_ context.Context, _ string, v Assessment) error {
	m.assessments[v.ID] = v
	return nil
}

// change applies a draft-only change, as pgStore.draftChange decides.
func (m *memoryStore) change(id string, apply func(v Assessment)) error {
	v, ok := m.assessments[id]
	switch {
	case !ok:
		return platform.ErrNotFound
	case v.Status != statusDraft:
		return errNotDraft
	}
	apply(v)
	return nil
}

func (m *memoryStore) updateDraft(_ context.Context, _ string, v Assessment) error {
	return m.change(v.ID, func(Assessment) { m.assessments[v.ID] = v })
}

func (m *memoryStore) publishDraft(_ context.Context, _, id string, at time.Time) error {
	return m.change(id, func(v Assessment) {
		v.Status, v.PublishedAt = statusPublished, &at
		m.assessments[id] = v
	})
}

func (m *memoryStore) deleteDraft(_ context.Context, _, id string) error {
	return m.change(id, func(Assessment) { delete(m.assessments, id) })
}

func (m *memoryStore) listAssessments(_ context.Context, _, _, student string, all bool, now time.Time) ([]Summary, error) {
	result := []Summary{}
	for _, v := range m.assessments {
		if all || v.visibleToStudents(now) {
			_, done := m.submitted[v.ID+"/"+student]
			result = append(result, Summary{ID: v.ID, Title: v.Title, Submitted: done, Status: v.Status, DueAt: v.DueAt})
		}
	}
	return result, nil
}

func (m *memoryStore) listProgress(context.Context, string, string) ([]Progress, error) {
	result := []Progress{}
	for _, v := range m.assessments {
		n := 0
		for key := range m.submitted {
			if strings.HasPrefix(key, v.ID+"/") {
				n++
			}
		}
		result = append(result, Progress{ID: v.ID, Title: v.Title, Submissions: n, Status: v.Status})
	}
	return result, nil
}

func (m *memoryStore) insertSubmission(_ context.Context, v Assessment, sub Submission) (bool, error) {
	key := v.ID + "/" + sub.Student
	if _, done := m.submitted[key]; done {
		return false, nil
	}
	m.submitted[key] = sub
	delete(m.saved, key)
	return true, nil
}

func (m *memoryStore) findSaved(_ context.Context, assessment, student string) (SavedAnswers, error) {
	answers, ok := m.saved[assessment+"/"+student]
	if !ok {
		return SavedAnswers{Answers: map[string]string{}}, nil
	}
	at := time.Now()
	return SavedAnswers{Answers: answers, SavedAt: &at}, nil
}

func (m *memoryStore) saveAnswers(_ context.Context, assessment, student string, answers map[string]string) (time.Time, error) {
	key := assessment + "/" + student
	if _, done := m.submitted[key]; done {
		return time.Time{}, errAlreadySubmitted
	}
	m.saved[key] = answers
	return time.Now(), nil
}

type fixedSkills skillStatus

func (s fixedSkills) check(*http.Request, string) skillStatus { return skillStatus(s) }

func newServer(relation string, skillAnswer skillStatus) (server, *memoryStore) {
	store := &memoryStore{assessments: map[string]Assessment{}, submitted: map[string]Submission{}, saved: map[string]map[string]string{}}
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
		{platform.RelationTeacher, skillValid, http.StatusCreated},
		{platform.RelationStudent, skillValid, http.StatusForbidden},
		{platform.RelationTeacher, skillInvalid, http.StatusUnprocessableEntity},
		{platform.RelationTeacher, skillUnavailable, http.StatusServiceUnavailable},
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
	if w.Code != http.StatusOK || strings.Contains(w.Body.String(), `"answer"`) {
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
	if w := call(t, s, http.MethodPost, path, body, "student"); w.Code != http.StatusCreated {
		t.Fatalf("status %d %s", w.Code, w.Body.String())
	}
	if w := call(t, s, http.MethodPost, path, body, "student"); w.Code != http.StatusConflict {
		t.Fatalf("second submission: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, path, `{"answers":{}}`, "student"); w.Code != http.StatusUnprocessableEntity && w.Code != http.StatusConflict {
		t.Fatalf("missing answers: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, path, body, "teacher", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a teacher submitted: %d", w.Code)
	}
}

func TestOnlyTheTeacherSeesSubmissionCounts(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	path := "/v1/assessments/" + v.ID + "/submissions"
	if w := call(t, s, http.MethodPost, path, `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student"); w.Code != http.StatusCreated {
		t.Fatalf("status %d", w.Code)
	}
	progress := "/v1/assessments/progress?classroom_id=" + room
	if w := call(t, s, http.MethodGet, progress, "", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student saw submission counts: %d", w.Code)
	}
	s.access = platformtest.Access(platform.RelationTeacher)
	w := call(t, s, http.MethodGet, progress, "", "teacher")
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"submissions":1`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestTheListSaysWhetherTheStudentSubmitted(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	list := "/v1/assessments?classroom_id=" + room
	if w := call(t, s, http.MethodGet, list, "", "student"); !strings.Contains(w.Body.String(), `"submitted":false`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	path := "/v1/assessments/" + v.ID + "/submissions"
	if w := call(t, s, http.MethodPost, path, `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student"); w.Code != http.StatusCreated {
		t.Fatalf("status %d", w.Code)
	}
	if w := call(t, s, http.MethodGet, list, "", "student"); !strings.Contains(w.Body.String(), `"submitted":true`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
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

func TestAMalformedOrUnknownAssessmentIsNotFound(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, skillValid)
	for _, id := range []string{"not-a-uuid", room} {
		if w := call(t, s, http.MethodGet, "/v1/assessments/"+id, "", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s: %d", id, w.Code)
		}
	}
}

const draftQuiz = `{"classroom_id":"` + room + `","title":"Draft","status":"draft","questions":[{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1","tolerance":"0","skill_id":"` + skill + `"}]}`

func TestADraftIsTheTeachersUntilPublished(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, skillValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", draftQuiz, "teacher")
	var v Assessment
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &v) != nil || v.Status != statusDraft || v.PublishedAt != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	path := "/v1/assessments/" + v.ID
	list := "/v1/assessments?classroom_id=" + room
	student := s
	student.access = platformtest.Access(platform.RelationStudent)
	if w = call(t, student, http.MethodGet, path, "", "student"); w.Code != http.StatusNotFound {
		t.Fatalf("a student opened a draft: %d", w.Code)
	}
	if w = call(t, student, http.MethodGet, list, "", "student"); strings.Contains(w.Body.String(), v.ID) {
		t.Fatalf("a student listed a draft: %s", w.Body.String())
	}
	if w = call(t, s, http.MethodGet, list, "", "teacher"); !strings.Contains(w.Body.String(), `"status":"draft"`) {
		t.Fatalf("the teacher did not list the draft: %s", w.Body.String())
	}
	edited := strings.Replace(draftQuiz, `"Draft"`, `"Edited"`, 1)
	if w = call(t, student, http.MethodPut, path, edited, "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student edited a draft: %d", w.Code)
	}
	moved := strings.Replace(edited, room, skill, 1)
	if w = call(t, s, http.MethodPut, path, moved, "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("a draft moved classroom: %d", w.Code)
	}
	if w = call(t, s, http.MethodPut, path, edited, "teacher"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"title":"Edited"`) {
		t.Fatalf("edit: %d %s", w.Code, w.Body.String())
	}
	if w = call(t, s, http.MethodPost, path+"/publish", "", "teacher"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"status":"published"`) {
		t.Fatalf("publish: %d %s", w.Code, w.Body.String())
	}
	if w = call(t, student, http.MethodGet, path, "", "student"); w.Code != http.StatusOK {
		t.Fatalf("a student could not open it once published: %d", w.Code)
	}
	for _, tc := range []struct{ method, path, body string }{{http.MethodPut, path, edited}, {http.MethodPost, path + "/publish", ""}, {http.MethodDelete, path, ""}} {
		if w = call(t, s, tc.method, tc.path, tc.body, "teacher"); w.Code != http.StatusConflict {
			t.Errorf("%s %s once published: %d", tc.method, tc.path, w.Code)
		}
	}
}

func TestOnlyADraftIsDeleted(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, skillValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", draftQuiz, "teacher")
	var v Assessment
	_ = json.Unmarshal(w.Body.Bytes(), &v)
	if w = call(t, s, http.MethodDelete, "/v1/assessments/"+v.ID, "", "teacher"); w.Code != http.StatusOK || len(store.assessments) != 0 {
		t.Fatalf("%d %v", w.Code, store.assessments)
	}
}

// scheduled creates a published assessment with the given extra JSON fields, as the classroom's teacher.
func scheduled(t *testing.T, fields string) (server, Assessment) {
	t.Helper()
	s, _ := newServer(platform.RelationTeacher, skillValid)
	body := strings.Replace(quiz, `"title":"Fractions",`, `"title":"Fractions",`+fields, 1)
	w := call(t, s, http.MethodPost, "/v1/assessments", body, "teacher")
	var v Assessment
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access(platform.RelationStudent)
	return s, v
}

func stamp(t time.Time) string { return `"` + t.UTC().Format(time.RFC3339) + `"` }

func TestStudentsWaitForTheOpeningDate(t *testing.T) {
	opens := time.Now().Add(time.Hour)
	s, v := scheduled(t, `"opens_at":`+stamp(opens)+`,`)
	if w := call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "student"); w.Code != http.StatusNotFound {
		t.Fatalf("opened early: %d", w.Code)
	}
	body := `{"answers":{"` + v.Questions[0].ID + `":"1"}}`
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", body, "student"); w.Code != http.StatusNotFound {
		t.Fatalf("submitted early: %d", w.Code)
	}
	s.clock = func() time.Time { return opens.Add(time.Minute) }
	if w := call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "student"); w.Code != http.StatusOK {
		t.Fatalf("not open after the opening date: %d", w.Code)
	}
}

func TestLateWorkIsFlaggedOrRefused(t *testing.T) {
	due := time.Now().Add(time.Hour)
	after := func() time.Time { return due.Add(time.Minute) }
	for policy, want := range map[string]int{lateAccept: http.StatusCreated, lateReject: http.StatusConflict} {
		s, v := scheduled(t, `"due_at":`+stamp(due)+`,"late_policy":"`+policy+`",`)
		s.clock = after
		body := `{"answers":{"` + v.Questions[0].ID + `":"1"}}`
		w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", body, "student")
		if w.Code != want || (want == http.StatusCreated && !strings.Contains(w.Body.String(), `"late":true`)) {
			t.Errorf("%s: %d %s", policy, w.Code, w.Body.String())
		}
	}
	s, _ := newServer(platform.RelationTeacher, skillValid)
	past := strings.Replace(quiz, `"title":"Fractions",`, `"title":"Fractions","due_at":`+stamp(time.Now().Add(-time.Hour))+`,`, 1)
	if w := call(t, s, http.MethodPost, "/v1/assessments", past, "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("published already past due: %d", w.Code)
	}
}

func TestSavedWorkUntilSubmission(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	path := "/v1/assessments/" + v.ID + "/saved-answers"
	if w := call(t, s, http.MethodGet, path, "", "student"); w.Code != http.StatusOK || w.Body.String() != `{"answers":{},"saved_at":null}`+"\n" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := call(t, s, http.MethodPut, path, `{"answers":{"elsewhere":"1"}}`, "student"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("saved an answer to another question: %d", w.Code)
	}
	saved := `{"answers":{"` + v.Questions[0].ID + `":"0.5"}}`
	if w := call(t, s, http.MethodPut, path, saved, "student"); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := call(t, s, http.MethodGet, path, "", "student"); !strings.Contains(w.Body.String(), `"0.5"`) {
		t.Fatalf("saved work lost: %s", w.Body.String())
	}
	if w := call(t, s, http.MethodPut, path, saved, "teacher"); w.Code != http.StatusForbidden {
		t.Fatalf("a teacher saved answers: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student"); w.Code != http.StatusCreated {
		t.Fatalf("%d", w.Code)
	}
	if w := call(t, s, http.MethodPut, path, saved, "student"); w.Code != http.StatusConflict {
		t.Fatalf("saved after submitting: %d", w.Code)
	}
}

func TestSubmissionsCarryTheGradingSettings(t *testing.T) {
	v := Assessment{ID: "a", Classroom: room, ReleasePolicy: releaseManual, AIMarking: true, CreatedBy: "teacher-1"}
	at := time.Date(2026, 10, 16, 8, 0, 0, 0, time.UTC)
	event := submittedEvent(v, Submission{ID: "s", Org: "org", Student: "student-1", Late: true, SubmittedAt: at})
	if event["release_policy"] != releaseManual || event["ai_marking"] != true || event["teacher_id"] != "teacher-1" || event["late"] != true || event["submitted_at"] != "2026-10-16T08:00:00Z" {
		t.Fatalf("%v", event)
	}
}
