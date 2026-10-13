package main

import (
	"encoding/json"
	"errors"
	"learning/platform"
	"net/http"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type server struct{ *platform.App }

func (s server) create(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var v Assessment
	if !platform.Decode(w, r, &v) {
		return
	}
	if err := v.Validate(); err != nil {
		platform.Error(w, 422, "VALIDATION_ERROR", err.Error())
		return
	}
	if !s.TeacherOf(w, r, v.Classroom) {
		return
	}
	checked := map[string]bool{}
	for i, q := range v.Questions {
		if !checked[q.Skill] {
			switch s.checkSkill(r, q.Skill) {
			case skillUnavailable:
				platform.Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Curriculum unavailable.")
				return
			case skillInvalid:
				platform.Error(w, 422, "INVALID_SKILL", "Every skill must exist in your curriculum.")
				return
			}
			checked[q.Skill] = true
		}
		v.Questions[i].ID = uuid.NewString()
	}
	v.ID = uuid.NewString()
	if err := s.insertAssessment(r.Context(), platform.Actor(r).Org, v); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 201, v)
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("classroom_id")
	if !s.Access(w, r, id, "") {
		return
	}
	result, err := s.listAssessments(r.Context(), platform.Actor(r).Org, id)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	v, err := s.findAssessment(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Assessment not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.Relation(w, r, v.Classroom, "")
	if !ok {
		return
	}
	// Answer keys go to the classroom's own teacher only, never on the strength of the token role.
	if relation == platform.RelationTeacher {
		platform.JSON(w, 200, v)
	} else {
		platform.JSON(w, 200, v.Public())
	}
}

func (s server) submit(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	if !c.Has("student") || c.Has("teacher") {
		platform.Error(w, 403, "FORBIDDEN", "Only enrolled students may submit.")
		return
	}
	v, err := s.findAssessment(r.Context(), c.Org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Assessment not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.Access(w, r, v.Classroom, c.Subject) {
		return
	}
	var input struct {
		Answers map[string]string `json:"answers"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !validateAnswers(v, input.Answers) {
		platform.Error(w, 422, "VALIDATION_ERROR", "Provide one valid answer for every question.")
		return
	}
	data, err := json.Marshal(input.Answers)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	id := uuid.NewString()
	inserted, err := s.insertSubmission(r.Context(), id, v, c.Org, c.Subject, data, input.Answers)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !inserted {
		platform.Error(w, 409, "ALREADY_SUBMITTED", "This assessment has already been submitted.")
		return
	}
	platform.JSON(w, 201, map[string]string{"id": id, "status": "submitted"})
}
