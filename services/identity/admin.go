package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"
	"unicode/utf8"

	"learning/platform"

	"github.com/google/uuid"
)

// adminRoutes are the routes a school's admins use to manage its accounts, classrooms and enrollment
// (adr 0019). every one is limited to the admin's own organization.
func (s server) adminRoutes(mux *http.ServeMux) {
	admin := func(handler http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if platform.Admin(w, r) {
				handler(w, r)
			}
		}
	}
	mux.HandleFunc("GET /v1/admin/accounts", admin(s.listAccounts))
	mux.HandleFunc("POST /v1/admin/accounts", admin(s.createAccount))
	mux.HandleFunc("POST /v1/admin/accounts/{id}/password", admin(s.resetPassword))
	mux.HandleFunc("GET /v1/admin/classrooms", admin(s.listSchoolClassrooms))
	mux.HandleFunc("POST /v1/admin/classrooms", admin(s.createClassroom))
	mux.HandleFunc("PATCH /v1/admin/classrooms/{id}", admin(s.updateClassroom))
	mux.HandleFunc("DELETE /v1/admin/classrooms/{id}", admin(s.deleteClassroom))
	mux.HandleFunc("GET /v1/admin/classrooms/{id}/students", admin(s.schoolRoster))
	mux.HandleFunc("GET /v1/admin/classrooms/{id}/student-ids", admin(s.schoolStudentIDs))
	mux.HandleFunc("POST /v1/admin/classrooms/{id}/enrollments", admin(s.enroll))
	mux.HandleFunc("DELETE /v1/admin/classrooms/{id}/enrollments/{student}", admin(s.unenroll))
}

// maxAccountPage is the most accounts one page of the school's list holds.
const maxAccountPage = 50

// listAccounts pages the school's teacher or student accounts (?role=), signed in or not, matching ?search=.
func (s server) listAccounts(w http.ResponseWriter, r *http.Request) {
	role := r.URL.Query().Get("role")
	if role != platform.RoleTeacher && role != platform.RoleStudent {
		platform.Invalid(w, "role must be teacher or student.")
		return
	}
	offset, limit, ok := platform.Page(w, r, 20, maxAccountPage)
	if !ok {
		return
	}
	search := strings.TrimSpace(r.URL.Query().Get("search"))
	if utf8.RuneCountInString(search) > maxName {
		platform.Invalid(w, "search must be at most 120 characters.")
		return
	}
	accounts, total, err := s.store.listAccounts(r.Context(), platform.Actor(r).Org, role, search, offset, limit)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"accounts": accounts, "total": total})
}

// createAccount makes a teacher or student sign-in account in the admin's school with a temporary password,
// answered once and never stored or logged, and records it so it can be assigned before its first sign-in. its
// user signs in as the school's id, a dot and the username (signInName), which the answer gives as username.
func (s server) createAccount(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Role     string `json:"role"`
		Username string `json:"username"`
		First    string `json:"first_name"`
		Last     string `json:"last_name"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	username, first, last := strings.ToLower(strings.TrimSpace(input.Username)), strings.TrimSpace(input.First), strings.TrimSpace(input.Last)
	switch {
	case input.Role != platform.RoleTeacher && input.Role != platform.RoleStudent:
		platform.Invalid(w, "role must be teacher or student.")
		return
	case !validUsername(username):
		platform.Invalid(w, "Username must have 3–64 lowercase letters, digits, dots, hyphens or underscores, starting with a letter or digit.")
		return
	case !validPersonName(first) || !validPersonName(last):
		platform.Invalid(w, "First and last name must each contain 1–60 characters.")
		return
	}
	org := platform.Actor(r).Org
	signIn, ok := signInName(org, username)
	if !ok {
		platform.Fail(w, errors.New("the organization id cannot start a username"))
		return
	}
	id, password, err := s.accounts.create(r.Context(), org, input.Role, signIn, first, last)
	if errors.Is(err, errUsernameTaken) {
		platform.Error(w, http.StatusConflict, "USERNAME_TAKEN", "Another account of your school already has that username.")
		return
	}
	if err != nil {
		platform.Unavailable(w, "The sign-in account could not be created; try again.")
		slog.Error("account_creation_failed", "error", err.Error())
		return
	}
	name := first + " " + last
	if err = s.store.insertAccount(r.Context(), org, id, input.Role, signIn, name); err != nil {
		// keycloak's account is removed again with a context of its own, so a cancelled request still cleans up.
		cleanup, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), 10*time.Second)
		defer cancel()
		if removed := s.accounts.remove(cleanup, id); removed != nil {
			slog.Error("account_rollback_failed", "account_id", id, "error", removed.Error())
		}
		platform.Fail(w, err)
		return
	}
	slog.Info("account_created", "account_id", id, "role", input.Role, "admin_id", platform.Actor(r).Subject)
	w.Header().Set("Cache-Control", "no-store")
	platform.JSON(w, http.StatusCreated, map[string]string{
		"id": id, "username": signIn, "display_name": name, "role": input.Role, "temporary_password": password,
	})
}

// resetPassword gives a teacher or student account of the admin's school a new temporary password, answered
// once and never stored or logged, as createAccount does. it is how an admin recovers an account whose
// creation answer was lost, or whose user forgot their password.
func (s server) resetPassword(w http.ResponseWriter, r *http.Request) {
	account, err := s.store.findAccount(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "No teacher or student account of your school has that id.") {
		return
	}
	password, err := s.accounts.resetPassword(r.Context(), account.ID)
	if errors.Is(err, errNoSignIn) {
		platform.NotFound(w, "That account has no sign-in account to reset.")
		return
	}
	if err != nil {
		platform.Unavailable(w, "The password could not be reset; try again.")
		slog.Error("password_reset_failed", "error", err.Error())
		return
	}
	slog.Info("password_reset", "account_id", account.ID, "admin_id", platform.Actor(r).Subject)
	w.Header().Set("Cache-Control", "no-store")
	platform.JSON(w, http.StatusOK, map[string]string{
		"id": account.ID, "username": account.Username, "display_name": account.Name, "role": account.Role, "temporary_password": password,
	})
}

// listSchoolClassrooms pages the school's classrooms with their teachers and student counts.
func (s server) listSchoolClassrooms(w http.ResponseWriter, r *http.Request) {
	offset, limit, ok := platform.Page(w, r, 20, 100)
	if !ok {
		return
	}
	classrooms, total, err := s.store.listOrgClassrooms(r.Context(), platform.Actor(r).Org, offset, limit)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, platform.ListPage[SchoolClassroom]{Items: classrooms, Total: total})
}

// createClassroom creates a classroom in the admin's school taught by one of its teachers.
func (s server) createClassroom(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Name    string `json:"name"`
		Teacher string `json:"teacher_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !validName(input.Name) {
		platform.Invalid(w, "Name must contain 1–120 characters.")
		return
	}
	id, name := uuid.NewString(), strings.TrimSpace(input.Name)
	err := s.store.insertClassroom(r.Context(), id, name, platform.Actor(r).Org, input.Teacher)
	if errors.Is(err, errNotATeacher) {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_TEACHER", "Choose a teacher of this school.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, Classroom{id, name, input.Teacher})
}

// updateClassroom renames a classroom of the admin's school or gives it another of the school's teachers,
// who then has its teacher's access everywhere, since every service asks identity.
func (s server) updateClassroom(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Name    *string `json:"name"`
		Teacher *string `json:"teacher_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if input.Name == nil && input.Teacher == nil {
		platform.Invalid(w, "Give a new name, a new teacher_id, or both.")
		return
	}
	if input.Name != nil {
		if !validName(*input.Name) {
			platform.Invalid(w, "Name must contain 1–120 characters.")
			return
		}
		trimmed := strings.TrimSpace(*input.Name)
		input.Name = &trimmed
	}
	id := r.PathValue("id")
	if !platform.ID(id) {
		platform.NotFound(w, "Classroom not found.")
		return
	}
	err := s.store.updateClassroom(r.Context(), platform.Actor(r).Org, id, input.Name, input.Teacher)
	if errors.Is(err, errNotATeacher) {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_TEACHER", "Choose a teacher of this school.")
		return
	}
	if !platform.Found(w, err, "Classroom not found.") {
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"classroom_id": id})
}

// schoolClassroom reports whether the classroom is a live classroom of the admin's school, answering 404
// when not, so admins learn nothing of other schools' classrooms.
func (s server) schoolClassroom(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("id")
	if !platform.ID(id) {
		platform.NotFound(w, "Classroom not found.")
		return "", false
	}
	org, _, err := s.store.findClassroomOwner(r.Context(), id)
	if errors.Is(err, platform.ErrNotFound) || (err == nil && org != platform.Actor(r).Org) {
		platform.NotFound(w, "Classroom not found.")
		return "", false
	}
	if err != nil {
		platform.Fail(w, err)
		return "", false
	}
	return id, true
}

// deleteClassroom deletes a classroom of the admin's school; every service then purges its data for it.
func (s server) deleteClassroom(w http.ResponseWriter, r *http.Request) {
	id, ok := s.schoolClassroom(w, r)
	if !ok {
		return
	}
	if err := s.store.deleteClassroom(r.Context(), id, platform.Actor(r).Org); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"classroom_id": id})
}

// schoolRoster lists one page of a classroom's students for the admin, as roster does for its teacher.
func (s server) schoolRoster(w http.ResponseWriter, r *http.Request) {
	id, ok := s.schoolClassroom(w, r)
	if !ok {
		return
	}
	s.rosterPage(w, r, id)
}

// schoolStudentIDs answers every enrolled student's id for the admin, as rosterIDs does for its teacher.
func (s server) schoolStudentIDs(w http.ResponseWriter, r *http.Request) {
	id, ok := s.schoolClassroom(w, r)
	if !ok {
		return
	}
	s.studentIDs(w, r, id)
}

// enroll adds a student of the school by account id or username.
func (s server) enroll(w http.ResponseWriter, r *http.Request) {
	id, ok := s.schoolClassroom(w, r)
	if !ok {
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
	err = s.store.insertEnrollment(r.Context(), id, student)
	if errors.Is(err, errStudentDeleted) {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_STUDENT", "That student's account was deleted.")
		return
	}
	if !platform.Found(w, err, "Classroom not found.") {
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"student_id": student, "classroom_id": id})
}

// unenroll removes a student from a classroom of the admin's school.
func (s server) unenroll(w http.ResponseWriter, r *http.Request) {
	id, ok := s.schoolClassroom(w, r)
	if !ok {
		return
	}
	student := r.PathValue("student")
	removed, err := s.store.removeEnrollment(r.Context(), id, platform.Actor(r).Org, student)
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
