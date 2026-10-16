package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"time"

	"github.com/google/uuid"
)

// server answers Assessment's HTTP routes: assessments, submissions and saved work in its store, classroom access
// from Identity and skills from Curriculum.
type server struct {
	store  store
	access platform.ClassroomAccess
	skills skills
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
	mux.HandleFunc("DELETE /v1/assessments/{id}", s.remove)
	mux.HandleFunc("POST /v1/assessments/{id}/publish", s.publish)
	mux.HandleFunc("POST /v1/assessments/{id}/submissions", s.submit)
	mux.HandleFunc("GET /v1/assessments/{id}/saved-answers", s.saved)
	mux.HandleFunc("PUT /v1/assessments/{id}/saved-answers", s.save)
}

// prepare fills in and checks an assessment a teacher sent, then checks its skills with Curriculum. On failure the
// response is written. Questions get new ids: nothing refers to a draft's questions yet.
func (s server) prepare(w http.ResponseWriter, r *http.Request, v *Assessment) bool {
	v.normalize()
	err := v.Validate()
	if err == nil && v.Status == statusPublished {
		err = v.publishable(s.now())
	}
	if err != nil {
		platform.Invalid(w, err.Error())
		return false
	}
	switch checkAll(r, s.skills, v.skillIDs()) {
	case skillUnavailable:
		platform.Unavailable(w, "Curriculum unavailable.")
		return false
	case skillInvalid:
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_SKILL", "Every skill must exist in your curriculum.")
		return false
	}
	for i := range v.Questions {
		v.Questions[i].ID = uuid.NewString()
	}
	v.PublishedAt = nil
	if v.Status == statusPublished {
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
	if !platform.TeacherOf(s.access, w, r, v.Classroom) || !s.prepare(w, r, &v) {
		return
	}
	v.ID = uuid.NewString()
	v.CreatedBy = platform.Actor(r).Subject
	if err := s.store.insertAssessment(r.Context(), platform.Actor(r).Org, v); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, v)
}

// update replaces a draft, and publishes it when the new version says so. A published assessment is immutable.
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
	if existing.Status != statusDraft {
		notDraft(w)
		return
	}
	if !s.prepare(w, r, &v) {
		return
	}
	v.ID, v.CreatedBy = existing.ID, existing.CreatedBy
	if !s.draftResult(w, s.store.updateDraft(r.Context(), platform.Actor(r).Org, v)) {
		return
	}
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

// remove deletes a draft. Published assessments are kept: students may have submitted them.
func (s server) remove(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	if !s.draftResult(w, s.store.deleteDraft(r.Context(), platform.Actor(r).Org, v.ID)) {
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

func notDraft(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "NOT_DRAFT", "A published assessment cannot be changed.")
}

// list is the classroom's assessments: all of them for its teacher; for a student, those published and open.
func (s server) list(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, id, "")
	if !ok {
		return
	}
	actor := platform.Actor(r)
	result, err := s.store.listAssessments(r.Context(), actor.Org, id, actor.Subject, relation == platform.RelationTeacher, s.now())
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// progress is, for the classroom's teacher only, each assessment with how many students have submitted it.
func (s server) progress(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	if !platform.TeacherOf(s.access, w, r, id) {
		return
	}
	result, err := s.store.listProgress(r.Context(), platform.Actor(r).Org, id)
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
	inserted, err := s.store.insertSubmission(r.Context(), v, sub)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !inserted {
		alreadySubmitted(w)
		return
	}
	platform.JSON(w, http.StatusCreated, map[string]any{"id": sub.ID, "status": "submitted", "late": sub.Late})
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
	case errors.Is(err, errAlreadySubmitted):
		alreadySubmitted(w)
	case err != nil:
		platform.Fail(w, err)
	default:
		platform.JSON(w, http.StatusOK, SavedAnswers{Answers: input.Answers, SavedAt: &at})
	}
}
