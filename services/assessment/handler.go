package main

import (
	"learning/platform"
	"net/http"

	"github.com/google/uuid"
)

// server answers Assessment's HTTP routes: assessments and submissions in its store, classroom access from
// Identity and skills from Curriculum.
type server struct {
	store  store
	access platform.ClassroomAccess
	skills skills
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/assessments", s.create)
	mux.HandleFunc("GET /v1/assessments", s.list)
	mux.HandleFunc("GET /v1/assessments/progress", s.progress)
	mux.HandleFunc("GET /v1/assessments/{id}", s.get)
	mux.HandleFunc("POST /v1/assessments/{id}/submissions", s.submit)
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
	if err := v.Validate(); err != nil {
		platform.Invalid(w, err.Error())
		return
	}
	if !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	switch checkAll(r, s.skills, v.skillIDs()) {
	case skillUnavailable:
		platform.Unavailable(w, "Curriculum unavailable.")
		return
	case skillInvalid:
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_SKILL", "Every skill must exist in your curriculum.")
		return
	}
	for i := range v.Questions {
		v.Questions[i].ID = uuid.NewString()
	}
	v.ID = uuid.NewString()
	if err := s.store.insertAssessment(r.Context(), platform.Actor(r).Org, v); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, v)
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	if !platform.Access(s.access, w, r, id, "") {
		return
	}
	actor := platform.Actor(r)
	result, err := s.store.listAssessments(r.Context(), actor.Org, id, actor.Subject)
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
	// Answer keys go to the classroom's own teacher only, never on the strength of the token role.
	if relation == platform.RelationTeacher {
		platform.JSON(w, http.StatusOK, v)
	} else {
		platform.JSON(w, http.StatusOK, v.Public())
	}
}

func (s server) submit(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	if !c.Has("student") || c.Has("teacher") {
		platform.Forbidden(w, "Only enrolled students may submit.")
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok {
		return
	}
	if !platform.Access(s.access, w, r, v.Classroom, c.Subject) {
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
	sub := Submission{ID: uuid.NewString(), Org: c.Org, Student: c.Subject, Answers: input.Answers}
	inserted, err := s.store.insertSubmission(r.Context(), v, sub)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !inserted {
		platform.Error(w, http.StatusConflict, "ALREADY_SUBMITTED", "This assessment has already been submitted.")
		return
	}
	platform.JSON(w, http.StatusCreated, map[string]string{"id": sub.ID, "status": "submitted"})
}
