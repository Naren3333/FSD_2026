package main

import (
	"learning/platform"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
)

// server answers Identity's HTTP routes from its store.
type server struct{ store store }

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/me", s.register)
	mux.HandleFunc("GET /v1/classrooms", s.list)
	mux.HandleFunc("POST /v1/classrooms", s.create)
	mux.HandleFunc("DELETE /v1/classrooms/{id}", s.remove)
	mux.HandleFunc("GET /v1/classrooms/{id}/access", s.access)
	mux.HandleFunc("POST /v1/classrooms/{id}/enrollments", s.enroll)
	mux.HandleFunc("DELETE /v1/classrooms/{id}/enrollments/{student}", s.unenroll)
	mux.HandleFunc("GET /v1/classrooms/{id}/students", s.roster)
	mux.HandleFunc("GET /v1/students", s.students)
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
	username, name := strings.TrimSpace(c.Username), displayName(c.Name, c.Username)
	if err := s.store.registerUser(r.Context(), c.Org, c.Subject, role, username, name); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"id": c.Subject, "org_id": c.Org, "role": role, "username": username, "display_name": name})
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

// remove deletes a classroom for its teacher. Students lose access and it leaves every list; its enrollments,
// submissions and grades are kept, and every service refuses access to it from then on.
func (s server) remove(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.owner(w, r, id, "delete it") {
		return
	}
	if err := s.store.deleteClassroom(r.Context(), id); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"classroom_id": id})
}

// owner reports whether the caller is the classroom's teacher, writing the refusal when not.
func (s server) owner(w http.ResponseWriter, r *http.Request, classroom, doing string) bool {
	if !platform.Teacher(w, r) {
		return false
	}
	relation, err := s.relation(r.Context(), platform.Actor(r), classroom, "")
	if err != nil {
		platform.Fail(w, err)
		return false
	}
	if relation != platform.RelationTeacher {
		platform.Forbidden(w, "Only the classroom teacher may "+doing+".")
		return false
	}
	return true
}

// enroll adds a student by account id or, as the web app does, by username.
func (s server) enroll(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.owner(w, r, id, "enroll students") {
		return
	}
	var input struct {
		Student  string `json:"student_id"`
		Username string `json:"username"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	student, problem, err := s.findStudent(r, input.Student, strings.TrimSpace(input.Username))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if problem != "" {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_STUDENT", problem)
		return
	}
	if err = s.store.insertEnrollment(r.Context(), id, student); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"student_id": student, "classroom_id": id})
}

// findStudent resolves exactly one of an account id or a username to a student in the caller's organization.
// problem explains a refusal.
func (s server) findStudent(r *http.Request, id, username string) (student, problem string, err error) {
	org := platform.Actor(r).Org
	switch {
	case (id == "") == (username == ""):
		return "", "Give either a student_id or a username.", nil
	case id != "":
		exists, err := s.store.studentExists(r.Context(), id, org)
		if err != nil || !exists {
			return "", "Student must first sign in to this organization.", err
		}
		return id, "", nil
	}
	ids, err := s.store.studentsNamed(r.Context(), org, username)
	switch {
	case err != nil:
		return "", "", err
	case len(ids) == 0:
		return "", "No student with that username has signed in to this organization.", nil
	case len(ids) > 1:
		return "", "More than one student has that username; enroll by account id.", nil
	}
	return ids[0], "", nil
}

func (s server) unenroll(w http.ResponseWriter, r *http.Request) {
	id, student := r.PathValue("id"), r.PathValue("student")
	if !s.owner(w, r, id, "remove students") {
		return
	}
	removed, err := s.store.removeEnrollment(r.Context(), id, student)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !removed {
		platform.NotFound(w, "That student is not enrolled in this classroom.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"student_id": student, "classroom_id": id})
}

// roster lists the classroom's students with their names, for its teacher.
func (s server) roster(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.owner(w, r, id, "see the roster") {
		return
	}
	students, err := s.store.listStudents(r.Context(), id)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, students)
}

// maxPage is the most students one page of the school's list holds.
const maxPage = 50

// students lists one page of the students in the teacher's organization who have signed in, so a teacher can find
// and enroll them: ?search= narrows by name or username, ?offset= and ?limit= (default 10, at most 50) choose the
// page, and total says how many match.
func (s server) students(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	query := r.URL.Query()
	offset, okOffset := number(query.Get("offset"), 0)
	limit, okLimit := number(query.Get("limit"), 10)
	if !okOffset || !okLimit || limit < 1 || limit > maxPage {
		platform.Invalid(w, "offset must be 0 or more and limit between 1 and 50.")
		return
	}
	search := strings.TrimSpace(query.Get("search"))
	if len(search) > maxName {
		platform.Invalid(w, "search must be at most 120 characters.")
		return
	}
	students, total, err := s.store.listOrgStudents(r.Context(), platform.Actor(r).Org, search, offset, limit)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"students": students, "total": total})
}

// number parses a non-negative whole-number query value, or gives fallback when it is absent.
func number(value string, fallback int) (int, bool) {
	if value == "" {
		return fallback, true
	}
	n, err := strconv.Atoi(value)
	return n, err == nil && n >= 0
}
