package main

import (
	"context"
	"encoding/json"
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
	deleted     []string         // the published assessments whose deletion would be announced
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

// change applies a change allowed until a student submits, as pgStore.changeUnsubmitted decides.
func (m *memoryStore) change(id string, apply func(v Assessment) error) error {
	v, ok := m.assessments[id]
	if !ok {
		return platform.ErrNotFound
	}
	for key := range m.submitted {
		if strings.HasPrefix(key, id+"/") {
			return errHasSubmissions
		}
	}
	return apply(v)
}

func (m *memoryStore) updateAssessment(_ context.Context, _ string, before, v Assessment) error {
	return m.change(v.ID, func(old Assessment) error {
		if !sameMarking(old, before) {
			return errAssessmentChanged
		}
		if old.Status == statusPublished && v.Status != statusPublished {
			return errNotDraft
		}
		m.assessments[v.ID] = v
		return nil
	})
}

func (m *memoryStore) publishDraft(_ context.Context, _, id string, at time.Time) error {
	v, ok := m.assessments[id]
	switch {
	case !ok:
		return platform.ErrNotFound
	case v.Status != statusDraft:
		return errNotDraft
	}
	v.Status, v.PublishedAt = statusPublished, &at
	m.assessments[id] = v
	return nil
}

func (m *memoryStore) updateDetails(_ context.Context, _, id string, d Details) error {
	v, ok := m.assessments[id]
	if !ok {
		return platform.ErrNotFound
	}
	v.Title, v.DueAt, v.LatePolicy = d.Title, d.DueAt, d.LatePolicy
	m.assessments[id] = v
	return nil
}

func (m *memoryStore) deleteAssessment(_ context.Context, _, id string, submissions bool) error {
	change := m.change
	if submissions {
		change = func(id string, apply func(v Assessment) error) error {
			v, ok := m.assessments[id]
			if !ok {
				return platform.ErrNotFound
			}
			return apply(v)
		}
	}
	return change(id, func(v Assessment) error {
		for key := range m.submitted {
			if strings.HasPrefix(key, id+"/") {
				delete(m.submitted, key)
			}
		}
		if v.Status == statusPublished {
			m.deleted = append(m.deleted, id)
		}
		delete(m.assessments, id)
		for key := range m.saved {
			if strings.HasPrefix(key, id+"/") {
				delete(m.saved, key)
			}
		}
		return nil
	})
}

func (m *memoryStore) listAssessments(_ context.Context, _, _, student string, all bool, enrolled []string, now time.Time, paging ...platform.ListWindow) ([]Summary, error) {
	result := []Summary{}
	for _, v := range m.assessments {
		if all || v.visibleToStudents(now) {
			_, done := m.submitted[v.ID+"/"+student]
			summary := Summary{ID: v.ID, Title: v.Title, Submitted: done, Status: v.Status, DueAt: v.DueAt}
			if all {
				n, has := 0, false
				for key := range m.submitted {
					if student, ok := strings.CutPrefix(key, v.ID+"/"); ok {
						has = true
						if slices.Contains(enrolled, student) {
							n++
						}
					}
				}
				summary.Submissions, summary.HasSubmissions = &n, &has
			}
			result = append(result, summary)
		}
	}
	return result, nil
}

func (m *memoryStore) listProgress(_ context.Context, _, _ string, enrolled []string, paging ...platform.ListWindow) ([]Progress, error) {
	result := []Progress{}
	for _, v := range m.assessments {
		n := 0
		for key := range m.submitted {
			if strings.HasPrefix(key, v.ID+"/") && slices.Contains(enrolled, strings.TrimPrefix(key, v.ID+"/")) {
				n++
			}
		}
		result = append(result, Progress{ID: v.ID, Title: v.Title, Submissions: n, Status: v.Status})
	}
	return result, nil
}

func (m *memoryStore) insertSubmission(_ context.Context, v Assessment, sub Submission) (inserted, late bool, err error) {
	current, ok := m.assessments[v.ID]
	switch {
	case !ok:
		return false, false, platform.ErrNotFound
	case !sameMarking(current, v):
		return false, false, errAssessmentChanged
	case current.closed(sub.SubmittedAt):
		return false, false, errClosed
	}
	key := v.ID + "/" + sub.Student
	if _, done := m.submitted[key]; done {
		return false, false, nil
	}
	sub.Late = current.late(sub.SubmittedAt)
	m.submitted[key] = sub
	m.events = append(m.events, submittedEvent(v, sub))
	delete(m.saved, key)
	return true, sub.Late, nil
}

func (m *memoryStore) findSubmission(_ context.Context, assessment, student string) (OwnSubmission, error) {
	v, ok := m.submitted[assessment+"/"+student]
	if !ok {
		return OwnSubmission{}, platform.ErrNotFound
	}
	return OwnSubmission{Answers: v.Answers, SubmittedAt: v.SubmittedAt, Late: v.Late}, nil
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
	if _, ok := m.assessments[assessment]; !ok {
		return time.Time{}, platform.ErrNotFound
	}
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
	return server{store: store, access: platformtest.Access(relation), roster: platformtest.Roster{"user"}, topics: fixedTopics(topicAnswer)}, store
}

func call(t *testing.T, s server, method, path, body string, roles ...string) *httptest.ResponseRecorder {
	t.Helper()
	return platformtest.Serve(s.routes, method, path, body, "user", roles...)
}

// step is one request of a scenario, sent to s with the caller's role, and the answer it expects: its status and, when
// set, a part of the body it has (want) or must not have (absent).
type step struct {
	name                     string
	s                        server
	method, path, body, role string
	status                   int
	want, absent             string
}

// run sends the steps in order and stops at the first answer that is not the one expected.
func run(t *testing.T, steps ...step) {
	t.Helper()
	for _, st := range steps {
		w := call(t, st.s, st.method, st.path, st.body, st.role)
		if w.Code != st.status || !strings.Contains(w.Body.String(), st.want) || (st.absent != "" && strings.Contains(w.Body.String(), st.absent)) {
			t.Fatalf("%s: %d %s", st.name, w.Code, w.Body.String())
		}
	}
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
		{platform.RelationTeacher, topicUnauthenticated, http.StatusUnauthorized},
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
	run(t,
		step{"submit", s, http.MethodPost, path, body, "student", http.StatusCreated, "", ""},
		step{"second submission", s, http.MethodPost, path, body, "student", http.StatusConflict, "", ""},
		step{"a teacher submitted", s, http.MethodPost, path, body, "teacher", http.StatusForbidden, "", ""},
	)
}

func TestOnlyTheTeacherSeesSubmissionCounts(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	progress, list := "/v1/assessments/progress?classroom_id="+room, "/v1/assessments?classroom_id="+room
	teacher := s
	teacher.access = platformtest.Access(platform.RelationTeacher)
	// Once the student is removed from the classroom, their kept submission is not counted, though the list still
	// says one was made.
	removed := teacher
	removed.roster = platformtest.Roster{}
	run(t,
		step{"submit", s, http.MethodPost, "/v1/assessments/" + v.ID + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusCreated, "", ""},
		step{"a student saw submission counts", s, http.MethodGet, progress, "", "student", http.StatusForbidden, "", ""},
		step{"progress", teacher, http.MethodGet, progress, "", "teacher", http.StatusOK, `"submissions":1`, ""},
		step{"progress after removal", removed, http.MethodGet, progress, "", "teacher", http.StatusOK, `"submissions":0`, ""},
		step{"list after removal", removed, http.MethodGet, list, "", "teacher", http.StatusOK, `"submissions":0,"has_submissions":true`, ""},
	)
}

func TestTheListSaysWhetherTheStudentSubmitted(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	list := "/v1/assessments?classroom_id=" + room
	run(t,
		step{"before", s, http.MethodGet, list, "", "student", http.StatusOK, `"submitted":false`, ""},
		step{"submit", s, http.MethodPost, "/v1/assessments/" + v.ID + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusCreated, "", ""},
		step{"after", s, http.MethodGet, list, "", "student", http.StatusOK, `"submitted":true`, ""},
	)
}

func TestAMalformedOrUnknownAssessmentIsNotFound(t *testing.T) {
	s, _ := newServer(platform.RelationTeacher, topicValid)
	run(t,
		step{"malformed", s, http.MethodGet, "/v1/assessments/not-a-uuid", "", "teacher", http.StatusNotFound, "", ""},
		step{"unknown", s, http.MethodGet, "/v1/assessments/" + room, "", "teacher", http.StatusNotFound, "", ""},
	)
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
	edited := strings.Replace(draftQuiz, `"Draft"`, `"Edited"`, 1)
	moved := strings.Replace(edited, room, topic, 1)
	run(t,
		step{"a student opened a draft", student, http.MethodGet, path, "", "student", http.StatusNotFound, "", ""},
		step{"a student listed a draft", student, http.MethodGet, list, "", "student", http.StatusOK, "", v.ID},
		step{"the teacher did not list the draft", s, http.MethodGet, list, "", "teacher", http.StatusOK, `"status":"draft"`, ""},
		step{"a student edited a draft", student, http.MethodPut, path, edited, "student", http.StatusForbidden, "", ""},
		step{"a draft moved classroom", s, http.MethodPut, path, moved, "teacher", http.StatusUnprocessableEntity, "", ""},
		step{"edit", s, http.MethodPut, path, edited, "teacher", http.StatusOK, `"title":"Edited"`, ""},
		step{"publish", s, http.MethodPost, path + "/publish", "", "teacher", http.StatusOK, `"status":"published"`, ""},
		step{"a student could not open it once published", student, http.MethodGet, path, "", "student", http.StatusOK, "", ""},
		step{"edited back to a draft once published", s, http.MethodPut, path, edited, "teacher", http.StatusConflict, "", ""},
		step{"published again", s, http.MethodPost, path + "/publish", "", "teacher", http.StatusConflict, "", ""},
	)
}

// twoQuestions is a published assessment of two questions, as a teacher would send it again with changes.
const twoQuestions = `{"classroom_id":"` + room + `","title":"Fractions","status":"published","questions":[` +
	`{"prompt":"1/2 + 1/2?","type":"numerical","options":[],"answer":"1","tolerance":"0","topic_id":"` + topic + `"},` +
	`{"prompt":"1/4 + 1/4?","type":"numerical","options":[],"answer":"0.5","tolerance":"0","topic_id":"` + topic + `"}]}`

func TestAPublishedAssessmentChangesUntilAStudentSubmits(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	w := call(t, s, http.MethodPost, "/v1/assessments", twoQuestions, "teacher")
	var v Assessment
	if w.Code != http.StatusCreated || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	path := "/v1/assessments/" + v.ID
	student := s
	student.access = platformtest.Access(platform.RelationStudent)
	first, second := v.Questions[0].ID, v.Questions[1].ID
	saved := `{"answers":{"` + first + `":"1","` + second + `":"0.5"}}`
	if w = call(t, student, http.MethodPut, path+"/saved-answers", saved, "student"); w.Code != http.StatusOK {
		t.Fatalf("save: %d %s", w.Code, w.Body.String())
	}

	// The teacher renames it and rewrites the second question: the first keeps its id and the work saved on it.
	changed := strings.Replace(strings.Replace(twoQuestions, "Fractions", "Halves", 1), "1/4 + 1/4?", "1/4 + 1/4 =", 1)
	var got Assessment
	if w = call(t, s, http.MethodPut, path, changed, "teacher"); w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &got) != nil {
		t.Fatalf("edit: %d %s", w.Code, w.Body.String())
	}
	if got.Status != statusPublished || got.PublishedAt == nil || !got.PublishedAt.Equal(*v.PublishedAt) || got.Title != "Halves" {
		t.Fatalf("the edit did not keep it published as it was: %+v", got)
	}
	if got.Questions[0].ID != first || got.Questions[1].ID == second {
		t.Fatalf("question ids: %s %s, were %s %s", got.Questions[0].ID, got.Questions[1].ID, first, second)
	}
	unchanged := strings.Replace(changed, `"status":"published",`, "", 1)
	run(t,
		step{"saved work after the edit", student, http.MethodGet, path + "/saved-answers", "", "student", http.StatusOK, first, second},
		// A student who opened it before the edit answers the old second question: they are told to open it again,
		// and so is one whose page autosaves work on it.
		step{"answers to a replaced question", student, http.MethodPost, path + "/submissions", saved, "student", http.StatusConflict, "ASSESSMENT_CHANGED", ""},
		step{"saved work on a replaced question", student, http.MethodPut, path + "/saved-answers", saved, "student", http.StatusConflict, "ASSESSMENT_CHANGED", ""},
		step{"an edit without a status", s, http.MethodPut, path, unchanged, "teacher", http.StatusOK, `"status":"published"`, ""},
	)

	// A student submits: the questions and grading settings are now fixed, and the assessment is kept.
	got = store.assessments[v.ID]
	answers := `{"answers":{"` + got.Questions[0].ID + `":"1","` + got.Questions[1].ID + `":"0.5"}}`
	run(t,
		step{"submit", student, http.MethodPost, path + "/submissions", answers, "student", http.StatusCreated, "", ""},
		step{"replaced after a submission", s, http.MethodPut, path, twoQuestions, "teacher", http.StatusConflict, "HAS_SUBMISSIONS", ""},
		step{"deleted after a submission", s, http.MethodDelete, path, "", "teacher", http.StatusConflict, "HAS_SUBMISSIONS", ""},
	)
	if !sameMarking(store.assessments[v.ID], got) {
		t.Fatal("a refused change changed the assessment")
	}

	// Its details still change: a new title, and a due date that is not already past.
	due := time.Now().Add(48 * time.Hour).UTC().Truncate(time.Second)
	details := `{"title":"Halves and quarters","due_at":` + stamp(due) + `,"late_policy":"reject"}`
	run(t, step{"details", s, http.MethodPatch, path, details, "teacher", http.StatusOK, `"title":"Halves and quarters"`, ""})
	if v := store.assessments[v.ID]; v.DueAt == nil || !v.DueAt.Equal(due) || v.LatePolicy != lateReject || !sameMarking(v, got) {
		t.Fatalf("details stored: %+v", v)
	}
	// Fields left out keep their values.
	run(t, step{"title only", s, http.MethodPatch, path, `{"title":"Only the title"}`, "teacher", http.StatusOK, "", ""})
	if v := store.assessments[v.ID]; v.Title != "Only the title" || v.DueAt == nil || !v.DueAt.Equal(due) || v.LatePolicy != lateReject {
		t.Fatalf("a title-only change changed the rest: %+v", v)
	}
	past := `{"title":"Halves","due_at":` + stamp(time.Now().Add(-time.Hour)) + `}`
	run(t,
		step{"a past due date", s, http.MethodPatch, path, past, "teacher", http.StatusUnprocessableEntity, "", ""},
		step{"no title", s, http.MethodPatch, path, `{"title":" "}`, "teacher", http.StatusUnprocessableEntity, "", ""},
		step{"a bad late policy", s, http.MethodPatch, path, `{"title":"x","late_policy":"never"}`, "teacher", http.StatusUnprocessableEntity, "", ""},
		step{"a student changed the details", student, http.MethodPatch, path, details, "student", http.StatusForbidden, "", ""},
	)
}

func TestDeletingASubmittedAssessmentNeedsConfirming(t *testing.T) {
	s, v := scheduled(t, "")
	store := s.store.(*memoryStore)
	path := "/v1/assessments/" + v.ID
	teacher := s
	teacher.access = platformtest.Access(platform.RelationTeacher)
	run(t,
		step{"submit", s, http.MethodPost, path + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusCreated, "", ""},
		step{"a student deleted it", s, http.MethodDelete, path + "?with_submissions=true", "", "student", http.StatusForbidden, "", ""},
		step{"without confirming", teacher, http.MethodDelete, path, "", "teacher", http.StatusConflict, "HAS_SUBMISSIONS", ""},
		step{"declining", teacher, http.MethodDelete, path + "?with_submissions=false", "", "teacher", http.StatusConflict, "HAS_SUBMISSIONS", ""},
		step{"confirmed", teacher, http.MethodDelete, path + "?with_submissions=true", "", "teacher", http.StatusOK, "", ""},
	)
	if len(store.assessments)+len(store.submitted) != 0 || !slices.Equal(store.deleted, []string{v.ID}) {
		t.Fatalf("left %v %v, announced %v", store.assessments, store.submitted, store.deleted)
	}
}

func TestTheTeacherSeesHowManySubmittedEach(t *testing.T) {
	s, v := scheduled(t, "")
	list := "/v1/assessments?classroom_id=" + room
	teacher := s
	teacher.access = platformtest.Access(platform.RelationTeacher)
	run(t,
		step{"submit", s, http.MethodPost, "/v1/assessments/" + v.ID + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusCreated, "", ""},
		// "submissions" is also part of "has_submissions".
		step{"a student saw the submission count", s, http.MethodGet, list, "", "student", http.StatusOK, "", "submissions"},
		step{"the teacher's list", teacher, http.MethodGet, list, "", "teacher", http.StatusOK, `"submissions":1,"has_submissions":true`, ""},
	)
}

func TestAPublishedAssessmentIsDeletedWithItsSavedWork(t *testing.T) {
	s, v := scheduled(t, "")
	store := s.store.(*memoryStore)
	path := "/v1/assessments/" + v.ID
	if w := call(t, s, http.MethodPut, path+"/saved-answers", `{"answers":{"`+v.Questions[0].ID+`":"1"}}`, "student"); w.Code != http.StatusOK {
		t.Fatalf("save: %d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access(platform.RelationTeacher)
	if w := call(t, s, http.MethodDelete, path, "", "teacher"); w.Code != http.StatusOK || len(store.assessments) != 0 || len(store.saved) != 0 {
		t.Fatalf("%d %v %v", w.Code, store.assessments, store.saved)
	}
}

func TestADraftIsDeleted(t *testing.T) {
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
	path := "/v1/assessments/" + v.ID
	later := s
	later.clock = func() time.Time { return opens.Add(time.Minute) }
	run(t,
		step{"opened early", s, http.MethodGet, path, "", "student", http.StatusNotFound, "", ""},
		step{"submitted early", s, http.MethodPost, path + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusNotFound, "", ""},
		step{"not open after the opening date", later, http.MethodGet, path, "", "student", http.StatusOK, "", ""},
	)
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
	saved := `{"answers":{"` + v.Questions[0].ID + `":"0.5"}}`
	run(t,
		step{"nothing saved yet", s, http.MethodGet, path, "", "student", http.StatusOK, `{"answers":{},"saved_at":null}`, ""},
		// An answer to a question it does not have was made against a version the teacher has since changed, as
		// for a submission.
		step{"saved an answer to another question", s, http.MethodPut, path, `{"answers":{"elsewhere":"1"}}`, "student", http.StatusConflict, "ASSESSMENT_CHANGED", ""},
		step{"saved no answers", s, http.MethodPut, path, `{}`, "student", http.StatusUnprocessableEntity, "", ""},
		step{"save", s, http.MethodPut, path, saved, "student", http.StatusOK, "", ""},
		step{"saved work lost", s, http.MethodGet, path, "", "student", http.StatusOK, `"0.5"`, ""},
		step{"a teacher saved answers", s, http.MethodPut, path, saved, "teacher", http.StatusForbidden, "", ""},
		step{"submit", s, http.MethodPost, "/v1/assessments/" + v.ID + "/submissions", `{"answers":{"` + v.Questions[0].ID + `":"1"}}`, "student", http.StatusCreated, "", ""},
		step{"saved after submitting", s, http.MethodPut, path, saved, "student", http.StatusConflict, "", ""},
	)
}

func TestOnlyTheSubmittingStudentReadsTheirSubmission(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	path := "/v1/assessments/" + v.ID + "/submission"
	answer := `{"answers":{"` + v.Questions[0].ID + `":"0.5"}}`
	run(t,
		step{"read before submitting", s, http.MethodGet, path, "", "student", http.StatusNotFound, "", ""},
		step{"submit", s, http.MethodPost, "/v1/assessments/" + v.ID + "/submissions", answer, "student", http.StatusCreated, "", ""},
		// The student's own answer, never the answer key ("1").
		step{"read", s, http.MethodGet, path, "", "student", http.StatusOK, `{"answers":{"` + v.Questions[0].ID + `":"0.5"},"submitted_at":"`, `"1"`},
		step{"read as a teacher", s, http.MethodGet, path, "", "teacher", http.StatusForbidden, "", ""},
		step{"an unknown assessment", s, http.MethodGet, "/v1/assessments/" + room + "/submission", "", "student", http.StatusNotFound, "", ""},
	)
	if w := call(t, s, http.MethodGet, path, "", "student"); !strings.Contains(w.Body.String(), `"late":false`) || strings.Contains(w.Body.String(), `"answer":`) {
		t.Fatalf("%s", w.Body.String())
	}
	// Another student of the classroom, who has not submitted, reads nothing of it.
	other := platformtest.ServeRequest(s.routes, platformtest.Request(platformtest.Claims("student-2", "student"), http.MethodGet, path, ""))
	if other.Code != http.StatusNotFound {
		t.Fatalf("another student: %d %s", other.Code, other.Body.String())
	}
	// Nor does a student who is not in the classroom.
	outside := s
	outside.access = platformtest.Access("")
	if w := call(t, outside, http.MethodGet, path, "", "student"); w.Code != http.StatusForbidden {
		t.Fatalf("a student outside the classroom: %d", w.Code)
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
	other := s
	other.access = platformtest.Access("") // another classroom's teacher
	run(t,
		step{"unknown id", s, http.MethodDelete, "/v1/assessments/" + room, "", "teacher", http.StatusNotFound, "", ""},
		step{"another teacher", other, http.MethodDelete, "/v1/assessments/" + draft.ID, "", "teacher", http.StatusForbidden, "", ""},
	)
	if len(store.assessments) != 1 {
		t.Fatalf("another teacher deleted it: %v", store.assessments)
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
	for topics, want := range map[topicStatus]int{topicInvalid: http.StatusUnprocessableEntity, topicUnavailable: http.StatusServiceUnavailable, topicUnauthenticated: http.StatusUnauthorized} {
		s, store := newServer(platform.RelationTeacher, topicValid)
		v := post(t, s, draftQuiz)
		s.topics = fixedTopics(topics)
		w := call(t, s, http.MethodPut, "/v1/assessments/"+v.ID, strings.Replace(draftQuiz, `"Draft"`, `"Edited"`, 1), "teacher")
		if w.Code != want || store.assessments[v.ID].Title != "Draft" {
			t.Errorf("%d: %d %s", topics, w.Code, w.Body.String())
		}
	}
}

func (m *memoryStore) countAssessments(ctx context.Context, org, classroom string, teacher bool, now time.Time) (int, error) {
	items, err := m.listAssessments(ctx, org, classroom, "", teacher, nil, now)
	return len(items), err
}

func (m *memoryStore) findAssessments(ctx context.Context, org string, ids []string) ([]Assessment, error) {
	items := []Assessment{}
	for _, id := range ids {
		if item, ok := m.assessments[id]; ok {
			items = append(items, item)
		}
	}
	return items, nil
}
