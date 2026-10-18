package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"
)

const (
	room  = "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"
	topic = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
)

type memoryStore struct {
	assessments map[string]Assessment
	submitted   map[string]Submission // by assessment/student
	saved       map[string]map[string]string
	events      []map[string]any // what each stored submission would publish, from what the handler passed
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
	m.events = append(m.events, submittedEvent(v, sub))
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

type fixedTopics topicStatus

func (s fixedTopics) check(*http.Request, string) topicStatus { return topicStatus(s) }

func newServer(relation string, topicAnswer topicStatus) (server, *memoryStore) {
	store := &memoryStore{assessments: map[string]Assessment{}, submitted: map[string]Submission{}, saved: map[string]map[string]string{}}
	return server{store: store, access: platformtest.Access(relation), topics: fixedTopics(topicAnswer)}, store
}

func call(t *testing.T, s server, method, path, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(s.routes, method, path, body, "user", roles...)
}

const quiz = `{"classroom_id":"` + room + `","title":"Fractions","questions":[{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1","tolerance":"0","topic_id":"` + topic + `"}]}`

func TestCreateChecksTheTeacherAndEveryTopic(t *testing.T) {
	cases := []struct {
		relation string
		topic    topicStatus
		status   int
	}{
		{platform.RelationTeacher, topicValid, http.StatusCreated},
		{platform.RelationStudent, topicValid, http.StatusForbidden},
		{platform.RelationTeacher, topicInvalid, http.StatusUnprocessableEntity},
		{platform.RelationTeacher, topicUnavailable, http.StatusServiceUnavailable},
	}
	for _, tc := range cases {
		s, _ := newServer(tc.relation, tc.topic)
		if w := call(t, s, http.MethodPost, "/v1/assessments", quiz, "teacher"); w.Code != tc.status {
			t.Errorf("%s/%d: %d %s", tc.relation, tc.topic, w.Code, w.Body.String())
		}
	}
}

func created(t *testing.T, relation string) (server, Assessment) {
	t.Helper()
	s, _ := newServer(platform.RelationTeacher, topicValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", quiz, "teacher")
	var v Assessment
	if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
		t.Fatal(err)
	}
	s.access = platformtest.Access(relation)
	return s, v
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
	if w := call(t, s, http.MethodPost, path, body, "teacher"); w.Code != http.StatusForbidden {
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
	s, _ := newServer(platform.RelationTeacher, topicValid)
	for _, id := range []string{"not-a-uuid", room} {
		if w := call(t, s, http.MethodGet, "/v1/assessments/"+id, "", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s: %d", id, w.Code)
		}
	}
}

const draftQuiz = `{"classroom_id":"` + room + `","title":"Draft","status":"draft","questions":[{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1","tolerance":"0","topic_id":"` + topic + `"}]}`

func TestADraftIsTheTeachersUntilPublished(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, topicValid)
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
	moved := strings.Replace(edited, room, topic, 1)
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
	s, store := newServer(platform.RelationTeacher, topicValid)
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
	s, _ := newServer(platform.RelationTeacher, topicValid)
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
	s, _ := newServer(platform.RelationTeacher, topicValid)
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

func TestMissingAnswersAreRefused(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	store := s.store.(*memoryStore)
	w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", `{"answers":{}}`, "student")
	if w.Code != http.StatusUnprocessableEntity || len(store.submitted) != 0 || len(store.events) != 0 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestSubmissionsCarryTheGradingSettings(t *testing.T) {
	due := time.Now().Add(time.Hour)
	s, v := scheduled(t, `"due_at":`+stamp(due)+`,"release_policy":"manual",`)
	store := s.store.(*memoryStore)
	at := due.Add(time.Minute).UTC()
	s.clock = func() time.Time { return at }
	body := `{"answers":{"` + v.Questions[0].ID + `":"1"}}`
	if w := platformtest.ServeRequest(s.routes, platformtest.Request(platformtest.Claims("student-1", "student"), http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", body)); w.Code != http.StatusCreated {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if len(store.events) != 1 {
		t.Fatalf("%d events", len(store.events))
	}
	event := store.events[0]
	if event["release_policy"] != releaseManual || event["ai_marking"] != false || event["teacher_id"] != "user" || event["late"] != true ||
		event["submitted_at"] != at.Format(time.RFC3339Nano) || event["student_id"] != "student-1" || event["org_id"] != "org" || event["assessment_id"] != v.ID {
		t.Fatalf("%v", event)
	}
}

func TestAnUpdateWithoutAStatusKeepsTheDraft(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, topicValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", draftQuiz, "teacher")
	var v Assessment
	if json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w = call(t, s, http.MethodPut, "/v1/assessments/"+v.ID, strings.Replace(draftQuiz, `"status":"draft",`, "", 1), "teacher"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"status":"draft"`) {
		t.Fatalf("an update without a status: %d %s", w.Code, w.Body.String())
	}
}

func TestASubmissionTooLargeToPublishIsRefused(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	essay := `{"prompt":"Explain.","type":"free_response","max_length":20000,"rubric":{"criteria":[{"title":"Clarity","levels":[{"points":0,"descriptor":"Unclear"},{"points":1,"descriptor":"Clear"}]}]},"topic_id":"` + topic + `"}`
	body := `{"classroom_id":"` + room + `","title":"Essays","questions":[` + strings.TrimSuffix(strings.Repeat(essay+",", 25), ",") + `]}`
	w := call(t, s, http.MethodPost, "/v1/assessments", body, "teacher")
	var v Assessment
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	answers := map[string]string{}
	for _, q := range v.Questions {
		answers[q.ID] = strings.Repeat("é", 20000) // two bytes each: 25 × 40 KB of JSON
	}
	data, _ := json.Marshal(map[string]any{"answers": answers})
	s.access = platformtest.Access(platform.RelationStudent)
	w = call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", string(data), "student")
	if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "SUBMISSION_TOO_LARGE") || len(store.submitted) != 0 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

// allTypes has one question of each type, a manual release and AI marking.
const allTypes = `{"classroom_id":"` + room + `","title":"Mixed","release_policy":"manual","ai_marking":true,"questions":[
	{"prompt":"Pick the sum","type":"multiple_choice","options":["half","whole"],"answer":"whole","tolerance":"","topic_id":"` + topic + `","origin":"generated","sources":[{"document_id":"` + room + `","chunk_id":"` + topic + `","page":2,"text":"Two halves make one whole."}]},
	{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1.00","tolerance":"0.01","points":2,"topic_id":"` + topic + `"},
	{"prompt":"Capital of France?","type":"short_answer","accepted_answers":["Paris"],"case_sensitive":true,"marking_scheme":[{"text":"Accept the city's name in French."}],"topic_id":"` + topic + `"},
	{"prompt":"Explain.","type":"free_response","max_length":300,"rubric":{"criteria":[{"title":"Clarity","levels":[{"points":0,"descriptor":"Unclear"},{"points":2,"descriptor":"Clear"}]}],"model_answer":"Halves add to a whole."},"marking_scheme":[{"document_id":"` + room + `","chunk_id":"` + topic + `","page":3,"text":"Award clarity for naming both halves."}],"topic_id":"` + topic + `"}]}`

// post creates an assessment as the classroom's teacher.
func post(t *testing.T, s server, body string) Assessment {
	t.Helper()
	w := call(t, s, http.MethodPost, "/v1/assessments", body, "teacher")
	var v Assessment
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	return v
}

// mixedAnswers answers every question of allTypes.
func mixedAnswers(v Assessment) string {
	answers := map[string]string{}
	for _, q := range v.Questions {
		answers[q.ID] = map[string]string{typeMultipleChoice: "half", typeNumerical: "1", typeShortAnswer: "Lyon", typeFreeResponse: "Because."}[q.Type]
	}
	data, _ := json.Marshal(map[string]any{"answers": answers})
	return string(data)
}

func snapshot(t *testing.T, m *memoryStore) string {
	t.Helper()
	data, err := json.Marshal([]any{m.assessments, m.submitted, m.saved, m.events})
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestStudentsSeeNoAnswerKey(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, allTypes)
	s.access = platformtest.Access(platform.RelationStudent)
	w := call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "student")
	var view map[string]any
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &view) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	viewKeys := []string{"id", "classroom_id", "title", "questions", "opens_at", "due_at", "late_policy"}
	for key := range view {
		if !slices.Contains(viewKeys, key) {
			t.Errorf("student view has %q", key)
		}
	}
	questions, _ := view["questions"].([]any)
	if len(questions) != 4 {
		t.Fatalf("%s", w.Body.String())
	}
	for _, q := range questions {
		for key := range q.(map[string]any) {
			if !slices.Contains([]string{"id", "prompt", "type", "options", "max_length", "points", "topic_id"}, key) {
				t.Errorf("student question has %q", key)
			}
		}
	}
	for _, secret := range []string{"Paris", "Halves add", "Clarity", "Unclear", "0.01", "1.00", "marking_scheme", "in French", "both halves", "sources", "generated", "make one whole"} {
		if strings.Contains(w.Body.String(), secret) {
			t.Errorf("leaked %q: %s", secret, w.Body.String())
		}
	}
	s.access = platformtest.Access(platform.RelationTeacher)
	if w = call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "teacher"); !strings.Contains(w.Body.String(), `"accepted_answers":["Paris"]`) || !strings.Contains(w.Body.String(), "Halves add") || !strings.Contains(w.Body.String(), "both halves") || !strings.Contains(w.Body.String(), `"origin":"generated"`) || !strings.Contains(w.Body.String(), "make one whole") {
		t.Fatalf("teacher view lacks the keys: %s", w.Body.String())
	}
}

func TestStrangersChangeNothing(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	published := post(t, s, quiz)
	draft := post(t, s, draftQuiz)
	answer := `{"answers":{"` + published.Questions[0].ID + `":"1"}}`
	cases := []struct {
		relation, role, method, path, body string
	}{
		{"", "teacher", http.MethodGet, "/v1/assessments/" + draft.ID, ""},
		{"", "student", http.MethodGet, "/v1/assessments/" + published.ID, ""},
		{"", "student", http.MethodGet, "/v1/assessments?classroom_id=" + room, ""},
		{"", "teacher", http.MethodGet, "/v1/assessments/progress?classroom_id=" + room, ""},
		{"", "teacher", http.MethodPut, "/v1/assessments/" + draft.ID, strings.Replace(draftQuiz, `"Draft"`, `"Taken"`, 1)},
		{"", "teacher", http.MethodPost, "/v1/assessments/" + draft.ID + "/publish", ""},
		{"", "teacher", http.MethodDelete, "/v1/assessments/" + draft.ID, ""},
		{"", "student", http.MethodPost, "/v1/assessments/" + published.ID + "/submissions", answer},
		{"", "student", http.MethodGet, "/v1/assessments/" + published.ID + "/saved-answers", ""},
		{"", "student", http.MethodPut, "/v1/assessments/" + published.ID + "/saved-answers", answer},
		{platform.RelationStudent, "student", http.MethodPut, "/v1/assessments/" + draft.ID, strings.Replace(draftQuiz, `"Draft"`, `"Taken"`, 1)},
		{platform.RelationStudent, "student", http.MethodPost, "/v1/assessments/" + draft.ID + "/publish", ""},
		{platform.RelationStudent, "student", http.MethodDelete, "/v1/assessments/" + draft.ID, ""},
	}
	before := snapshot(t, store)
	for _, tc := range cases {
		s.access = platformtest.Access(tc.relation)
		if w := call(t, s, tc.method, tc.path, tc.body, tc.role); w.Code != http.StatusForbidden {
			t.Errorf("%q %s %s %s: %d %s", tc.relation, tc.role, tc.method, tc.path, w.Code, w.Body.String())
		}
		if after := snapshot(t, store); after != before {
			t.Fatalf("%q %s %s %s changed the store", tc.relation, tc.role, tc.method, tc.path)
		}
	}
}

func TestDeletingNeedsAKnownDraftAndItsTeacher(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	draft := post(t, s, draftQuiz)
	if w := call(t, s, http.MethodDelete, "/v1/assessments/"+room, "", "teacher"); w.Code != http.StatusNotFound {
		t.Fatalf("unknown id: %d", w.Code)
	}
	s.access = platformtest.Access("") // another classroom's teacher
	if w := call(t, s, http.MethodDelete, "/v1/assessments/"+draft.ID, "", "teacher"); w.Code != http.StatusForbidden || len(store.assessments) != 1 {
		t.Fatalf("another teacher: %d", w.Code)
	}
}

func TestPublishingAfterTheDueDateIsRefused(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	due := time.Now().Add(time.Hour)
	v := post(t, s, strings.Replace(draftQuiz, `"status":"draft",`, `"status":"draft","due_at":`+stamp(due)+`,`, 1))
	s.clock = func() time.Time { return due.Add(time.Minute) }
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/publish", "", "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if got := store.assessments[v.ID]; got.Status != statusDraft || got.PublishedAt != nil {
		t.Fatalf("%+v", got)
	}
}

func TestAnUpdateCanPublish(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, draftQuiz)
	path := "/v1/assessments/" + v.ID
	w := call(t, s, http.MethodPut, path, strings.Replace(draftQuiz, `"status":"draft"`, `"status":"published"`, 1), "teacher")
	var got Assessment
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &got) != nil || got.Status != statusPublished || got.PublishedAt == nil || store.assessments[v.ID].PublishedAt == nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access(platform.RelationStudent)
	if w = call(t, s, http.MethodGet, path, "", "student"); w.Code != http.StatusOK {
		t.Fatalf("a student could not open it: %d", w.Code)
	}
}

func TestSavingIsClosedWithTheAssessment(t *testing.T) {
	due := time.Now().Add(time.Hour)
	s, v := scheduled(t, `"due_at":`+stamp(due)+`,"late_policy":"reject",`)
	store := s.store.(*memoryStore)
	s.clock = func() time.Time { return due.Add(time.Minute) }
	w := call(t, s, http.MethodPut, "/v1/assessments/"+v.ID+"/saved-answers", `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student")
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "CLOSED") || len(store.saved) != 0 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestADraftTakesNoSubmission(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, draftQuiz)
	s.access = platformtest.Access(platform.RelationStudent)
	w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student")
	if w.Code != http.StatusNotFound || len(store.submitted) != 0 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestTheCreatorPaysEvenAfterAnotherTeacherEdits(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, draftQuiz)
	if store.assessments[v.ID].CreatedBy != "user" {
		t.Fatalf("created by %q", store.assessments[v.ID].CreatedBy)
	}
	r := platformtest.Request(platformtest.Claims("co-teacher", "teacher"), http.MethodPut, "/v1/assessments/"+v.ID, draftQuiz)
	if w := platformtest.ServeRequest(s.routes, r); w.Code != http.StatusOK || store.assessments[v.ID].CreatedBy != "user" {
		t.Fatalf("%d, created by %q", w.Code, store.assessments[v.ID].CreatedBy)
	}
}

func TestAnUpdateChecksItsTopics(t *testing.T) {
	for topics, want := range map[topicStatus]int{topicInvalid: http.StatusUnprocessableEntity, topicUnavailable: http.StatusServiceUnavailable} {
		s, store := newServer(platform.RelationTeacher, topicValid)
		v := post(t, s, draftQuiz)
		s.topics = fixedTopics(topics)
		w := call(t, s, http.MethodPut, "/v1/assessments/"+v.ID, strings.Replace(draftQuiz, `"Draft"`, `"Edited"`, 1), "teacher")
		if w.Code != want || store.assessments[v.ID].Title != "Draft" {
			t.Errorf("%d: %d %s", topics, w.Code, w.Body.String())
		}
	}
}
