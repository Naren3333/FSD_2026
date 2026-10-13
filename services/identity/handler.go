package main

import (
	"learning/platform"
	"net/http"
	"strings"

	"github.com/google/uuid"
)

// server answers Identity's HTTP routes from its store.
type server struct{ store store }

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/me", s.register)
	mux.HandleFunc("GET /v1/classrooms", s.list)
	mux.HandleFunc("POST /v1/classrooms", s.create)
	mux.HandleFunc("GET /v1/classrooms/{id}/access", s.access)
	mux.HandleFunc("POST /v1/classrooms/{id}/enrollments", s.enroll)
}

func (s server) register(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	role := "student"
	if c.Has("teacher") {
		role = "teacher"
	} else if !c.Has("student") {
		platform.Forbidden(w, "An application role is required.")
		return
	}
	if err := s.store.registerUser(r.Context(), c.Org, c.Subject, role); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"id": c.Subject, "org_id": c.Org, "role": role})
}

func (s server) access(w http.ResponseWriter, r *http.Request) {
	relation, err := s.relation(r.Context(), platform.Actor(r), r.PathValue("id"), r.URL.Query().Get("student_id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if relation == "" {
		platform.Forbidden(w, "Classroom or student access denied.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"allowed": true, "relation": relation})
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
		platform.Invalid(w, "Name must contain 1–120 characters.")
		return
	}
	c := platform.Actor(r)
	id := uuid.NewString()
	if err := s.store.insertClassroom(r.Context(), id, strings.TrimSpace(input.Name), c.Org, c.Subject); err != nil {
		platform.Error(w, http.StatusConflict, "PROFILE_REQUIRED", "Complete sign-in registration before creating a classroom.")
		return
	}
	platform.JSON(w, http.StatusCreated, Classroom{id, input.Name, c.Subject})
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	result, err := s.store.listClassrooms(r.Context(), c.Org, c.Subject, c.Has("teacher"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
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
	if relation != platform.RelationTeacher {
		platform.Forbidden(w, "Only the classroom teacher may enroll students.")
		return
	}
	var input struct {
		Student string `json:"student_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	exists, err := s.store.studentExists(r.Context(), input.Student, c.Org)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !exists {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_STUDENT", "Student must first sign in to this organization.")
		return
	}
	if err = s.store.insertEnrollment(r.Context(), id, input.Student); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"student_id": input.Student, "classroom_id": id})
}
