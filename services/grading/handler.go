package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"strings"
)

// server answers Grading's HTTP routes from its store, with classroom access from Identity.
type server struct {
	store  store
	access platform.ClassroomAccess
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/evaluations", s.list)
	mux.HandleFunc("GET /v1/evaluations/mine", s.mine)
	mux.HandleFunc("GET /v1/evaluations/counts", s.counts)
	mux.HandleFunc("POST /v1/evaluations/{id}/finalize", s.finalize)
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	result, err := s.store.listEvaluations(r.Context(), platform.Actor(r).Org, classroom)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// counts is, for the classroom's teacher only, how many of its evaluations need grading and how many are final.
func (s server) counts(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	result, err := s.store.countEvaluations(r.Context(), platform.Actor(r).Org, classroom)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// mine is the calling student's finalized results in a classroom they are enrolled in.
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
	result, err := s.store.listFinalized(r.Context(), c.Org, classroom, c.Subject)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

func (s server) finalize(w http.ResponseWriter, r *http.Request) {
	// The role check comes first so a student is refused before any lookup; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Evaluation not found.")
		return
	}
	var input struct {
		Reason    string         `json:"reason"`
		Overrides map[string]int `json:"overrides"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if strings.TrimSpace(input.Reason) == "" || len(input.Reason) > 1000 {
		platform.Invalid(w, "A review reason of 1–1000 characters is required.")
		return
	}
	classroom, err := s.store.findClassroom(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Evaluation not found.") {
		return
	}
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	v, err := s.store.finalizeEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Reason, input.Overrides)
	switch {
	case errors.Is(err, errAlreadyFinalized):
		platform.Error(w, http.StatusConflict, "ALREADY_FINALIZED", "This evaluation has already been finalized.")
		return
	case errors.Is(err, errScoreOutOfBounds):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Scores must be within question bounds.")
		return
	case errors.Is(err, errUnknownQuestion):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Override references an unknown question.")
		return
	case err != nil:
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, v)
}
