package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"strings"
)

// server answers identity's http routes from its store, creating sign-in accounts through accounts.
type server struct {
	store    store
	accounts accounts
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/me", s.register)
	mux.HandleFunc("DELETE /v1/me", s.deleteAccount)
	mux.HandleFunc("GET /v1/classrooms", s.list)
	mux.HandleFunc("GET /v1/classrooms/page", s.list)
	mux.HandleFunc("GET /v1/classrooms/{id}/access", s.access)
	mux.HandleFunc("GET /v1/classrooms/{id}/students", s.roster)
	mux.HandleFunc("GET /v1/classrooms/{id}/student-ids", s.rosterIDs)
	s.adminRoutes(mux)
}

func (s server) register(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	var role string
	// a token with more than one of these roles never reaches here (platform.Authenticator.Wrap).
	switch {
	case c.Has(platform.RoleAdmin):
		role = platform.RoleAdmin
	case c.Has(platform.RoleTeacher):
		role = platform.RoleTeacher
	case c.Has(platform.RoleStudent):
		role = platform.RoleStudent
	default:
		platform.Forbidden(w, "An application role is required.")
		return
	}
	username, name := strings.TrimSpace(c.Username), displayName(c.Name, c.Username)
	err := s.store.registerUser(r.Context(), c.Org, c.Subject, role, username, name)
	if errors.Is(err, errAccountDeleted) {
		platform.Error(w, http.StatusForbidden, "ACCOUNT_DELETED", "This account was deleted and cannot be used again.")
		return
	}
	if errors.Is(err, errRoleChanged) {
		platform.Error(w, http.StatusConflict, "ROLE_CONFLICT", "This account is registered with another role; an account keeps the role it was created with.")
		return
	}
	if err != nil {
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

func (s server) list(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	teacher := c.Has(platform.RoleTeacher)
	platform.ServeList(w, r, 100, func(paging ...platform.ListWindow) ([]Classroom, error) {
		return s.store.listClassrooms(r.Context(), c.Org, c.Subject, teacher, paging...)
	}, func() (int, error) { return s.store.countClassrooms(r.Context(), c.Org, c.Subject, teacher) })
}

// owner reports whether the caller is the classroom's teacher, who may read its roster, writing the refusal
// when not.
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

// findStudent resolves exactly one of an account id or a username to a student in the caller's
// organization. problem explains a refusal.
func (s server) findStudent(r *http.Request, id, username string) (student, problem string, err error) {
	org := platform.Actor(r).Org
	switch {
	case (id == "") == (username == ""):
		return "", "Give either a student_id or a username.", nil
	case id != "":
		exists, err := s.store.studentExists(r.Context(), id, org)
		if err != nil || !exists {
			return "", "No student account of this school has that id.", err
		}
		return id, "", nil
	}
	ids, err := s.store.studentsNamed(r.Context(), org, username)
	switch {
	case err != nil:
		return "", "", err
	case len(ids) == 0:
		return "", "No student account of this school has that username.", nil
	case len(ids) > 1:
		return "", "More than one student has that username; enroll by account id.", nil
	}
	return ids[0], "", nil
}

// roster lists one page of the classroom's students with their names, for its teacher to read: ?offset= and ?limit=
// (default 50, at most 500) choose the page, and total says how many are enrolled.
func (s server) roster(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.owner(w, r, id, "see the roster") {
		return
	}
	s.rosterPage(w, r, id)
}

// rosterPage answers one page of the classroom's roster.
func (s server) rosterPage(w http.ResponseWriter, r *http.Request, id string) {
	offset, limit, ok := platform.Page(w, r, 50, maxRosterPage)
	if !ok {
		return
	}
	students, total, err := s.store.listStudents(r.Context(), id, offset, limit)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"students": students, "total": total})
}

// maxRosterPage is the most students one page of a classroom's named roster holds.
const maxRosterPage = 500

// rosterIDs lists the ids of every student enrolled in the classroom, for its teacher.
func (s server) rosterIDs(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !s.owner(w, r, id, "see the roster") {
		return
	}
	s.studentIDs(w, r, id)
}

// studentIDs answers the id of every student enrolled in the classroom.
func (s server) studentIDs(w http.ResponseWriter, r *http.Request, id string) {
	ids, err := s.store.listStudentIDs(r.Context(), id)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, ids)
}

// deleteAccount deletes the caller's own account (adr 0011): it can never sign in or register again, and
// every service deletes the user's data.
func (s server) deleteAccount(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	if c.Has(platform.RoleAdmin) {
		platform.Error(w, http.StatusForbidden, "ADMIN_ACCOUNT", "A school admin account cannot delete itself; another admin of the school manages it in Keycloak.")
		return
	}
	if !platform.ID(c.Subject) { // every keycloak account id is a uuid, and the event's resource must be one
		platform.NotFound(w, "This account is not registered or was already deleted.")
		return
	}
	err := s.store.deleteAccount(r.Context(), c.Org, c.Subject)
	if errors.Is(err, errAdminAccount) {
		platform.Error(w, http.StatusForbidden, "ADMIN_ACCOUNT", "A school admin account cannot delete itself; another admin of the school manages it in Keycloak.")
		return
	}
	if errors.Is(err, errOwnsClassrooms) {
		platform.Error(w, http.StatusConflict, "OWNS_CLASSROOMS", "Ask your school admin to give your classrooms to another teacher or delete them first.")
		return
	}
	if !platform.Found(w, err, "This account is not registered or was already deleted.") {
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
