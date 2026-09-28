package main

import (
	"context"
	_ "embed"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
	"strings"
)

//go:embed migrations/001_init.sql
var migration string

type Classroom struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Teacher string `json:"teacher_id"`
}
type server struct{ *platform.App }

func validName(name string) bool { return strings.TrimSpace(name) != "" && len(name) <= 120 }
func (s server) register(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	role := "student"
	if c.Has("teacher") {
		role = "teacher"
	} else if !c.Has("student") {
		platform.Error(w, 403, "FORBIDDEN", "An application role is required.")
		return
	}
	tx, err := s.DB.Begin(r.Context())
	if err != nil {
		platform.Fail(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	_, err = tx.Exec(r.Context(), "INSERT INTO organizations(id) VALUES($1) ON CONFLICT DO NOTHING", c.Org)
	if err == nil {
		_, err = tx.Exec(r.Context(), "INSERT INTO users(id,org_id,role) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET role=EXCLUDED.role WHERE users.org_id=EXCLUDED.org_id", c.Subject, c.Org, role)
	}
	if err == nil {
		err = tx.Commit(r.Context())
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, map[string]string{"id": c.Subject, "org_id": c.Org, "role": role})
}
func (s server) allowed(ctx context.Context, c platform.Claims, classroom, student string) (bool, error) {
	if !platform.ID(classroom) {
		return false, nil
	}
	var org, teacher string
	err := s.DB.QueryRow(ctx, "SELECT org_id,teacher_id FROM classrooms WHERE id=$1", classroom).Scan(&org, &teacher)
	if err == pgx.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if org != c.Org {
		return false, nil
	}
	owner := c.Has("teacher") && teacher == c.Subject
	if !owner && (!c.Has("student") || (student != "" && student != c.Subject)) {
		return false, nil
	}
	target := student
	if !owner {
		target = c.Subject
	}
	if target == "" {
		return owner, nil
	}
	var enrolled bool
	err = s.DB.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM enrollments WHERE classroom_id=$1 AND student_id=$2)", classroom, target).Scan(&enrolled)
	return enrolled, err
}
func (s server) access(w http.ResponseWriter, r *http.Request) {
	ok, err := s.allowed(r.Context(), platform.Actor(r), r.PathValue("id"), r.URL.Query().Get("student_id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !ok {
		platform.Error(w, 403, "FORBIDDEN", "Classroom or student access denied.")
		return
	}
	platform.JSON(w, 200, map[string]bool{"allowed": true})
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
	_, err := s.DB.Exec(r.Context(), "INSERT INTO classrooms(id,name,org_id,teacher_id) VALUES($1,$2,$3,$4)", id, strings.TrimSpace(input.Name), c.Org, c.Subject)
	if err != nil {
		platform.Error(w, 409, "PROFILE_REQUIRED", "Complete sign-in registration before creating a classroom.")
		return
	}
	platform.JSON(w, 201, Classroom{id, input.Name, c.Subject})
}
func (s server) list(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	rows, err := s.DB.Query(r.Context(), `SELECT c.id,c.name,c.teacher_id FROM classrooms c WHERE c.org_id=$1 AND ((c.teacher_id=$2 AND $3) OR EXISTS(SELECT 1 FROM enrollments e WHERE e.classroom_id=c.id AND e.student_id=$2)) ORDER BY c.created_at DESC LIMIT 100`, c.Org, c.Subject, c.Has("teacher"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	defer rows.Close()
	result := []Classroom{}
	for rows.Next() {
		var v Classroom
		if err = rows.Scan(&v.ID, &v.Name, &v.Teacher); err != nil {
			platform.Fail(w, err)
			return
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		platform.Fail(w, rows.Err())
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
	ok, err := s.allowed(r.Context(), c, id, "")
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !ok {
		platform.Error(w, 403, "FORBIDDEN", "Only the classroom teacher may enroll students.")
		return
	}
	var input struct {
		Student string `json:"student_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	var exists bool
	err = s.DB.QueryRow(r.Context(), "SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND role='student')", input.Student, c.Org).Scan(&exists)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !exists {
		platform.Error(w, 422, "INVALID_STUDENT", "Student must first sign in to this organization.")
		return
	}
	_, err = s.DB.Exec(r.Context(), "INSERT INTO enrollments(classroom_id,student_id) VALUES($1,$2) ON CONFLICT DO NOTHING", id, input.Student)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, map[string]string{"student_id": input.Student, "classroom_id": id})
}
func main() {
	a, err := platform.New("identity", migration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("POST /v1/me", s.register)
	a.Mux.HandleFunc("GET /v1/classrooms", s.list)
	a.Mux.HandleFunc("POST /v1/classrooms", s.create)
	a.Mux.HandleFunc("GET /v1/classrooms/{id}/access", s.access)
	a.Mux.HandleFunc("POST /v1/classrooms/{id}/enrollments", s.enroll)
	if err = a.Run(); err != nil {
		slog.Error("server_failed")
		os.Exit(1)
	}
}
