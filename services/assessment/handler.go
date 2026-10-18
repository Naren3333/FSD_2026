package main

import (
	"encoding/json"
	"errors"
	"learning/platform"
	"net/http"
	"time"

	"github.com/google/uuid"
)

// server answers Assessment's HTTP routes: assessments, submissions and saved work in its store, classroom access
// from Identity and topics from Curriculum.
type server struct {
	store  store
	access platform.ClassroomAccess
	roster platform.ClassroomRoster
	topics topics
	clock  func() time.Time // time.Now unless a test sets it
}

func (s server) now() time.Time {
	if s.clock != nil {
		return s.clock()
	}
	return time.Now()
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/assessments", s.create)
	mux.HandleFunc("GET /v1/assessments", s.list)
	mux.HandleFunc("GET /v1/assessments/progress", s.progress)
	mux.HandleFunc("GET /v1/assessments/{id}", s.get)
	mux.HandleFunc("PUT /v1/assessments/{id}", s.update)
	mux.HandleFunc("PATCH /v1/assessments/{id}", s.details)
	mux.HandleFunc("DELETE /v1/assessments/{id}", s.remove)
	mux.HandleFunc("POST /v1/assessments/{id}/publish", s.publish)
	mux.HandleFunc("POST /v1/assessments/{id}/submissions", s.submit)
	mux.HandleFunc("GET /v1/assessments/{id}/saved-answers", s.saved)
	mux.HandleFunc("PUT /v1/assessments/{id}/saved-answers", s.save)
}

// prepare fills in and checks an assessment a teacher sent, then checks its topics with Curriculum. On failure the
// response is written. Questions get new ids, except those unchanged from the published assessment it replaces
// (before), on which students may have saved work. Publishing needs a due date in the future, and so does a new due
// date on an assessment already published.
func (s server) prepare(w http.ResponseWriter, r *http.Request, v *Assessment, before *Assessment) bool {
	v.normalize()
	err := v.Validate()
	if err == nil && v.Status == statusPublished && (before == nil || before.Status != statusPublished || !sameTime(before.DueAt, v.DueAt)) {
		err = v.publishable(s.now())
	}
	if err != nil {
		platform.Invalid(w, err.Error())
		return false
	}
	switch checkAll(r, s.topics, v.topicIDs()) {
	case topicUnavailable:
		platform.Unavailable(w, "Curriculum unavailable.")
		return false
	case topicInvalid:
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_TOPIC", "Every topic must exist in your curriculum.")
		return false
	}
	for i := range v.Questions {
		v.Questions[i].ID = uuid.NewString()
	}
	v.PublishedAt = nil
	switch {
	case before != nil && before.Status == statusPublished:
		v.keepQuestionIDs(before.Questions)
		v.PublishedAt = before.PublishedAt
	case v.Status == statusPublished:
		at := s.now().UTC()
		v.PublishedAt = &at
	}
	return true
}

func (s server) create(w http.ResponseWriter, r *http.Request) {
	// The role check comes first so a student is refused before validation; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return
	}
	var v Assessment
	if !platform.Decode(w, r, &v) {
		return
	}
	if !platform.ID(v.Classroom) {
		platform.Invalid(w, "provide a classroom, title, and 1–50 questions")
		return
	}
	if !platform.TeacherOf(s.access, w, r, v.Classroom) || !s.prepare(w, r, &v, nil) {
		return
	}
	v.ID = uuid.NewString()
	v.CreatedBy = platform.Actor(r).Subject
	if err := s.store.insertAssessment(r.Context(), platform.Actor(r).Org, v); err != nil {
		if errors.Is(err, errClassroomPurged) {
			// Identity deleted the classroom after the access check.
			platform.NotFound(w, "Classroom not found.")
			return
		}
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, v)
}

// update replaces an assessment until a student submits it, and publishes a draft when the new version says so;
// without a status it keeps the one it has. A published assessment stays published, and once a student has submitted
// it only its details change (PATCH).
func (s server) update(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	existing, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, existing.Classroom) {
		return
	}
	var v Assessment
	if !platform.Decode(w, r, &v) {
		return
	}
	if v.Classroom != existing.Classroom {
		platform.Invalid(w, "An assessment cannot move to another classroom.")
		return
	}
	if v.Status == "" {
		v.Status = existing.Status // publishing is irreversible, so only an explicit status publishes
	}
	if existing.Status == statusPublished && v.Status != statusPublished {
		notDraft(w)
		return
	}
	if !s.prepare(w, r, &v, &existing) {
		return
	}
	v.ID, v.CreatedBy = existing.ID, existing.CreatedBy
	if !s.changeResult(w, s.store.updateAssessment(r.Context(), platform.Actor(r).Org, existing, v)) {
		return
	}
	platform.JSON(w, http.StatusOK, v)
}

// details changes an assessment's title, due date and late policy. Unlike a replacement it is allowed after students
// have submitted: grading does not depend on them. A field left out keeps its value; a due_at of null removes the due
// date.
func (s server) details(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	var change struct {
		Title      *string         `json:"title"`
		DueAt      json.RawMessage `json:"due_at"` // nil when left out, "null" to remove the due date
		LatePolicy *string         `json:"late_policy"`
	}
	if !platform.Decode(w, r, &change) {
		return
	}
	d := Details{Title: v.Title, DueAt: v.DueAt, LatePolicy: v.LatePolicy}
	if change.Title != nil {
		d.Title = *change.Title
	}
	if change.LatePolicy != nil {
		d.LatePolicy = *change.LatePolicy
	}
	if change.DueAt != nil {
		d.DueAt = nil // a fresh value, so decoding never writes through to v's
		if json.Unmarshal(change.DueAt, &d.DueAt) != nil {
			platform.Invalid(w, "Provide the due date as a date-time, or null for none.")
			return
		}
	}
	if err := v.validateDetails(d, s.now()); err != nil {
		platform.Invalid(w, err.Error())
		return
	}
	if !platform.Found(w, s.store.updateDetails(r.Context(), platform.Actor(r).Org, v.ID, d), "Assessment not found.") {
		return
	}
	v.Title, v.DueAt, v.LatePolicy = d.Title, d.DueAt, d.LatePolicy
	platform.JSON(w, http.StatusOK, v)
}

func (s server) publish(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	if v.Status != statusDraft {
		notDraft(w)
		return
	}
	now := s.now().UTC()
	if err := v.publishable(now); err != nil {
		platform.Invalid(w, err.Error())
		return
	}
	if !s.draftResult(w, s.store.publishDraft(r.Context(), platform.Actor(r).Org, v.ID, now)) {
		return
	}
	v.Status, v.PublishedAt = statusPublished, &now
	platform.JSON(w, http.StatusOK, v)
}

// remove deletes an assessment. Once a student has submitted it, only when the teacher confirms with
// ?with_submissions=true that its submissions and every grade for them go too (ADR 0008); without it the request is
// refused, so a deletion never takes students' grades by accident.
func (s server) remove(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	withSubmissions := r.URL.Query().Get("with_submissions") == "true"
	err := s.store.deleteAssessment(r.Context(), platform.Actor(r).Org, v.ID, withSubmissions)
	if errors.Is(err, errHasSubmissions) {
		platform.Error(w, http.StatusConflict, "HAS_SUBMISSIONS", "Students have submitted this assessment. Deleting it also deletes their submissions and grades; confirm that to delete it.")
		return
	}
	if !s.draftResult(w, err) {
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"id": v.ID})
}

// draftResult writes the response for a failed draft change: 409 once published (a race with another tab, say),
// 404 when it is gone.
func (s server) draftResult(w http.ResponseWriter, err error) bool {
	if errors.Is(err, errNotDraft) {
		notDraft(w)
		return false
	}
	return platform.Found(w, err, "Assessment not found.")
}

// changeResult writes the response for a failed replacement: 409 once a student has submitted it, once published
// (when it would unpublish it) or when another change landed first, 404 when it is gone.
func (s server) changeResult(w http.ResponseWriter, err error) bool {
	if errors.Is(err, errAssessmentChanged) {
		platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "This assessment changed while you were saving. Open it again and make your changes to it as it is now.")
		return false
	}
	if errors.Is(err, errHasSubmissions) {
		platform.Error(w, http.StatusConflict, "HAS_SUBMISSIONS", "Students have submitted this assessment, so its questions and grading settings can no longer change. You can still change its title, due date and late policy.")
		return false
	}
	return s.draftResult(w, err)
}

func notDraft(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "NOT_DRAFT", "A published assessment cannot be published again or go back to being a draft.")
}

// list is the classroom's assessments: all of them for its teacher; for a student, those published and open.
func (s server) list(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, id, "")
	if !ok {
		return
	}
	actor := platform.Actor(r)
	teacher := relation == platform.RelationTeacher
	var enrolled []string
	if teacher {
		// The teacher sees how many enrolled students submitted: work kept for students since removed isn't counted.
		if enrolled, ok = s.roster.Roster(w, r, id); !ok {
			return
		}
	}
	result, err := s.store.listAssessments(r.Context(), actor.Org, id, actor.Subject, teacher, enrolled, s.now())
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// progress is, for the classroom's teacher only, each assessment with how many of its enrolled students have
// submitted it.
func (s server) progress(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	if !platform.TeacherOf(s.access, w, r, id) {
		return
	}
	enrolled, ok := s.roster.Roster(w, r, id)
	if !ok {
		return
	}
	result, err := s.store.listProgress(r.Context(), platform.Actor(r).Org, id, enrolled)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// findAssessment looks up the assessment the path names in the caller's organization. On failure the response is
// written: 404 for a malformed or unknown id.
func (s server) findAssessment(w http.ResponseWriter, r *http.Request) (Assessment, bool) {
	id := r.PathValue("id")
	if !platform.ID(id) {
		platform.NotFound(w, "Assessment not found.")
		return Assessment{}, false
	}
	v, err := s.store.findAssessment(r.Context(), platform.Actor(r).Org, id)
	return v, platform.Found(w, err, "Assessment not found.")
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	v, ok := s.findAssessment(w, r)
	if !ok {
		return
	}
	relation, ok := s.access.Relation(w, r, v.Classroom, "")
	if !ok {
		return
	}
	// Answer keys and rubrics go to the classroom's own teacher only, never on the strength of the token role.
	switch {
	case relation == platform.RelationTeacher:
		platform.JSON(w, http.StatusOK, v)
	case v.visibleToStudents(s.now()):
		platform.JSON(w, http.StatusOK, v.Public())
	default:
		platform.NotFound(w, "Assessment not found.")
	}
}

// forStudent finds the assessment for an enrolled student who may answer it: published and open. On failure the
// response is written; a draft or one not open yet is not found.
func (s server) forStudent(w http.ResponseWriter, r *http.Request) (Assessment, bool) {
	c := platform.Actor(r)
	if !c.Has("student") || c.Has("teacher") {
		platform.Forbidden(w, "Only enrolled students may submit.")
		return Assessment{}, false
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.Access(s.access, w, r, v.Classroom, c.Subject) {
		return v, false
	}
	if !v.visibleToStudents(s.now()) {
		platform.NotFound(w, "Assessment not found.")
		return v, false
	}
	return v, true
}

func closed(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "CLOSED", "This assessment is past its due date and no longer takes submissions.")
}

func (s server) submit(w http.ResponseWriter, r *http.Request) {
	v, ok := s.forStudent(w, r)
	if !ok {
		return
	}
	var input struct {
		Answers map[string]string `json:"answers"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !answersCurrent(v, input.Answers) {
		assessmentChanged(w) // answers to questions the teacher has since replaced
		return
	}
	if !validateAnswers(v, input.Answers) {
		platform.Invalid(w, "Provide one valid answer for every question.")
		return
	}
	now := s.now().UTC()
	if v.closed(now) {
		closed(w)
		return
	}
	c := platform.Actor(r)
	sub := Submission{ID: uuid.NewString(), Org: c.Org, Student: c.Subject, Answers: input.Answers, Late: v.late(now), SubmittedAt: now}
	if submittedEventTooLarge(v, sub) {
		platform.Error(w, http.StatusUnprocessableEntity, "SUBMISSION_TOO_LARGE", "These answers are too long to submit together. Shorten the longest ones and try again.")
		return
	}
	inserted, late, err := s.store.insertSubmission(r.Context(), v, sub)
	switch {
	case errors.Is(err, errClosed):
		closed(w) // the teacher changed the due date or late policy meanwhile
		return
	case errors.Is(err, errAccountDeleted):
		platform.Forbidden(w, "This account was deleted.")
		return
	case errors.Is(err, errAssessmentChanged):
		assessmentChanged(w)
		return
	case errors.Is(err, platform.ErrNotFound):
		platform.NotFound(w, "Assessment not found.") // deleted while the student was answering
		return
	case err != nil:
		platform.Fail(w, err)
		return
	}
	if !inserted {
		alreadySubmitted(w)
		return
	}
	platform.JSON(w, http.StatusCreated, map[string]any{"id": sub.ID, "status": "submitted", "late": late})
}

func assessmentChanged(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "Your teacher changed this assessment while you were answering it. Open it again to see the questions as they are now; your answers to questions that did not change are kept.")
}

func alreadySubmitted(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "ALREADY_SUBMITTED", "This assessment has already been submitted.")
}

// saved is the calling student's unsubmitted work on the assessment; none once they have submitted.
func (s server) saved(w http.ResponseWriter, r *http.Request) {
	v, ok := s.forStudent(w, r)
	if !ok {
		return
	}
	result, err := s.store.findSaved(r.Context(), v.ID, platform.Actor(r).Subject)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	// Work saved on questions the teacher has since changed or removed no longer answers anything.
	for id, answer := range result.Answers {
		if !validateSaved(v, map[string]string{id: answer}) {
			delete(result.Answers, id)
		}
	}
	platform.JSON(w, http.StatusOK, result)
}

// save keeps the calling student's work in progress, so a long answer survives a closed tab. Saved work may be
// incomplete; it is checked in full only when submitted.
func (s server) save(w http.ResponseWriter, r *http.Request) {
	v, ok := s.forStudent(w, r)
	if !ok {
		return
	}
	var input struct {
		Answers map[string]string `json:"answers"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !answersCurrent(v, input.Answers) {
		assessmentChanged(w) // work on questions the teacher has since replaced, as for a submission
		return
	}
	if input.Answers == nil || !validateSaved(v, input.Answers) {
		platform.Invalid(w, "Save answers to this assessment's questions, each within its length limit.")
		return
	}
	if v.closed(s.now()) {
		closed(w)
		return
	}
	at, err := s.store.saveAnswers(r.Context(), v.ID, platform.Actor(r).Subject, input.Answers)
	switch {
	case errors.Is(err, errAccountDeleted):
		platform.Forbidden(w, "This account was deleted.")
	case errors.Is(err, errAlreadySubmitted):
		alreadySubmitted(w)
	case errors.Is(err, platform.ErrNotFound):
		platform.NotFound(w, "Assessment not found.") // deleted while the student was answering
	case err != nil:
		platform.Fail(w, err)
	default:
		platform.JSON(w, http.StatusOK, SavedAnswers{Answers: input.Answers, SavedAt: &at})
	}
}
