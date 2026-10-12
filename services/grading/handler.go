package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
)

type server struct{ *platform.App }

func (s server) list(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	classroom := r.URL.Query().Get("classroom_id")
	if !s.Access(w, r, classroom, "") {
		return
	}
	result, err := s.listEvaluations(r.Context(), platform.Actor(r).Org, classroom)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) finalize(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Evaluation not found.")
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
		platform.Error(w, 422, "VALIDATION_ERROR", "A review reason of 1–1000 characters is required.")
		return
	}
	classroom, err := s.findClassroom(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Evaluation not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.Access(w, r, classroom, "") {
		return
	}
	v, err := s.finalizeEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Reason, input.Overrides)
	switch {
	case errors.Is(err, errAlreadyFinalized):
		platform.Error(w, 409, "ALREADY_FINALIZED", "This evaluation has already been finalized.")
		return
	case errors.Is(err, errScoreOutOfBounds):
		platform.Error(w, 422, "INVALID_OVERRIDE", "Scores must be within question bounds.")
		return
	case errors.Is(err, errUnknownQuestion):
		platform.Error(w, 422, "INVALID_OVERRIDE", "Override references an unknown question.")
		return
	case err != nil:
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, v)
}
