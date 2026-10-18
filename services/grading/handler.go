package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"strings"
	"unicode/utf8"
)

// server answers Grading's HTTP routes from its store, with classroom access from Identity.
type server struct {
	store  store
	access platform.ClassroomAccess
	roster platform.ClassroomRoster
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/evaluations", s.list)
	mux.HandleFunc("GET /v1/evaluations/mine", s.mine)
	mux.HandleFunc("GET /v1/evaluations/counts", s.counts)
	mux.HandleFunc("POST /v1/evaluations/release", s.release)
	mux.HandleFunc("POST /v1/evaluations/{id}/marks", s.mark)
	mux.HandleFunc("POST /v1/evaluations/{id}/finalize", s.finalize)
}

// list is, for the classroom's teacher only, its evaluations with the students' answers, or one assessment's. Only
// enrolled students' are listed: a removed student's evaluations are kept, and come back if they are enrolled again.
func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	assessment := r.URL.Query().Get("assessment_id")
	if assessment != "" && !platform.ID(assessment) {
		platform.Invalid(w, "assessment_id must be an assessment identifier.")
		return
	}
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	students, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return
	}
	result, err := s.store.listEvaluations(r.Context(), platform.Actor(r).Org, classroom, assessment, students)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// counts is, for the classroom's teacher only, how many of its enrolled students' evaluations need grading and how
// many are final.
func (s server) counts(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	students, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return
	}
	result, err := s.store.countEvaluations(r.Context(), platform.Actor(r).Org, classroom, students)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// mine is the calling student's released results in a classroom they are enrolled in.
func (s server) mine(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	classroom := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, classroom, c.Subject)
	if !ok {
		return
	}
	if relation != platform.RelationStudent {
		platform.Forbidden(w, "Only enrolled students have results here.")
		return
	}
	result, err := s.store.listReleased(r.Context(), c.Org, classroom, c.Subject)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	own := make([]OwnResult, len(result))
	for i, v := range result {
		own[i] = v.own()
	}
	platform.JSON(w, http.StatusOK, own)
}

// teacherOfEvaluation checks the caller is the teacher of the classroom of the evaluation the path names. On
// failure the response is written.
func (s server) teacherOfEvaluation(w http.ResponseWriter, r *http.Request) bool {
	// The role check comes first so a student is refused before any lookup; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return false
	}
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Evaluation not found.")
		return false
	}
	classroom, err := s.store.findClassroom(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Evaluation not found.") {
		return false
	}
	return platform.TeacherOf(s.access, w, r, classroom)
}

// mark saves the teacher's marks on a draft without finalizing it.
func (s server) mark(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Marks map[string]Mark `json:"marks"`
	}
	if !s.teacherOfEvaluation(w, r) || !platform.Decode(w, r, &input) {
		return
	}
	if len(input.Marks) == 0 {
		platform.Invalid(w, "Give marks for at least one question.")
		return
	}
	v, err := s.store.saveMarks(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Marks)
	if writeMarkingError(w, err) {
		platform.JSON(w, http.StatusOK, v)
	}
}

func (s server) finalize(w http.ResponseWriter, r *http.Request) {
	if !s.teacherOfEvaluation(w, r) {
		return
	}
	var input struct {
		Reason    string          `json:"reason"`
		Overrides map[string]int  `json:"overrides"`
		Marks     map[string]Mark `json:"marks"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if strings.TrimSpace(input.Reason) == "" || utf8.RuneCountInString(input.Reason) > 1000 {
		platform.Invalid(w, "A review reason of 1–1000 characters is required.")
		return
	}
	marks := overridesAsMarks(input.Overrides, input.Marks)
	v, err := s.store.finalizeEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Reason, marks)
	if writeMarkingError(w, err) {
		platform.JSON(w, http.StatusOK, v)
	}
}

// writeMarkingError writes the response for a failed marking or finalization and reports whether err was nil.
func writeMarkingError(w http.ResponseWriter, err error) bool {
	switch {
	case err == nil:
		return true
	case errors.Is(err, errAlreadyFinalized):
		platform.Error(w, http.StatusConflict, "ALREADY_FINALIZED", "This evaluation has already been finalized.")
	case errors.Is(err, errUnmarked):
		platform.Error(w, http.StatusUnprocessableEntity, "NEEDS_MARKING", "Mark every question before finalizing.")
	case errors.Is(err, errScoreOutOfBounds):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Scores must be within question bounds.")
	case errors.Is(err, errUnknownQuestion):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Override references an unknown question.")
	case errors.Is(err, errInvalidCriteria):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_MARK", "Give a free response one level for every rubric criterion, and other questions points.")
	case errors.Is(err, errFeedbackTooLong):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_MARK", "Feedback is at most 2000 characters.")
	default:
		platform.Found(w, err, "Evaluation not found.")
	}
	return false
}

// release shows an assessment's finalized grades to its students, for the classroom's teacher only.
func (s server) release(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var input struct {
		Assessment string `json:"assessment_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !platform.ID(input.Assessment) {
		platform.Invalid(w, "assessment_id must be an assessment identifier.")
		return
	}
	org := platform.Actor(r).Org
	classroom, err := s.store.assessmentClassroom(r.Context(), org, input.Assessment)
	if !platform.Found(w, err, "No grades for this assessment.") || !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	n, err := s.store.release(r.Context(), org, input.Assessment, platform.Actor(r).Subject)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"assessment_id": input.Assessment, "released": n})
}
