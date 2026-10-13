package main

import (
	"learning/platform"
	"net/http"
	"strings"

	"github.com/google/uuid"
)

type server struct{ *platform.App }

func (s server) register(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	role := "student"
	if c.Has("teacher") {
		role = "teacher"
	} else if !c.Has("student") {
		platform.Error(w, 403, "FORBIDDEN", "An application role is required.")
		return
	}
	if err := s.registerUser(r.Context(), c.Org, c.Subject, role); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, map[string]string{"id": c.Subject, "org_id": c.Org, "role": role})
}

func (s server) access(w http.ResponseWriter, r *http.Request) {
	relation, err := s.relation(r.Context(), platform.Actor(r), r.PathValue("id"), r.URL.Query().Get("student_id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if relation == "" {
		platform.Error(w, 403, "FORBIDDEN", "Classroom or student access denied.")
		return
	}
	platform.JSON(w, 200, map[string]any{"allowed": true, "relation": relation})
}

func (s server) create(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var input struct {
		Name string `json:"name"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !validName(input.Name) {
		platform.Error(w, 422, "VALIDATION_ERROR", "Name must contain 1–120 characters.")
		return
	}
	c := platform.Actor(r)
	id := uuid.NewString()
	if err := s.insertClassroom(r.Context(), id, strings.TrimSpace(input.Name), c.Org, c.Subject); err != nil {
		platform.Error(w, 409, "PROFILE_REQUIRED", "Complete sign-in registration before creating a classroom.")
		return
	}
	platform.JSON(w, 201, Classroom{id, input.Name, c.Subject})
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	result, err := s.listClassrooms(r.Context(), c.Org, c.Subject, c.Has("teacher"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) enroll(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	c := platform.Actor(r)
	id := r.PathValue("id")
	relation, err := s.relation(r.Context(), c, id, "")
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if relation != relationTeacher {
		platform.Error(w, 403, "FORBIDDEN", "Only the classroom teacher may enroll students.")
		return
	}
	var input struct {
		Student string `json:"student_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	exists, err := s.studentExists(r.Context(), input.Student, c.Org)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !exists {
		platform.Error(w, 422, "INVALID_STUDENT", "Student must first sign in to this organization.")
		return
	}
	if err = s.insertEnrollment(r.Context(), id, input.Student); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, map[string]string{"student_id": input.Student, "classroom_id": id})
}
