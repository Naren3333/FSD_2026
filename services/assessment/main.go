package main

import (
	_ "embed"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
)

//go:embed migrations/001_init.sql
var migration string

type server struct{ *platform.App }

func (s server) load(r *http.Request, id string) (Assessment, error) {
	var v Assessment
	if !platform.ID(id) {
		return v, pgx.ErrNoRows
	}
	var data []byte
	err := s.DB.QueryRow(r.Context(), "SELECT id,classroom_id,title,questions FROM assessments WHERE id=$1 AND org_id=$2", id, platform.Actor(r).Org).Scan(&v.ID, &v.Classroom, &v.Title, &data)
	if err == nil {
		err = json.Unmarshal(data, &v.Questions)
	}
	return v, err
}
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
	if !s.Access(w, r, v.Classroom, "") {
		return
	}
	checked := map[string]bool{}
	for i, q := range v.Questions {
		if !checked[q.Skill] {
			req, err := http.NewRequestWithContext(r.Context(), "GET", platform.Required("CURRICULUM_URL")+"/v1/nodes/"+q.Skill, nil)
			if err != nil {
				platform.Fail(w, err)
				return
			}
			req.Header.Set("Authorization", r.Header.Get("Authorization"))
			req.Header.Set("X-Correlation-ID", platform.Correlation(r.Context()))
			res, err := s.Client.Do(req)
			if err != nil {
				platform.Error(w, 503, "DEPENDENCY_UNAVAILABLE", "Curriculum unavailable.")
				return
			}
			var node struct {
				ID   string `json:"id"`
				Kind string `json:"kind"`
			}
			err = json.NewDecoder(res.Body).Decode(&node)
			res.Body.Close()
			if res.StatusCode != 200 || err != nil || node.Kind != "skill" {
				platform.Error(w, 422, "INVALID_SKILL", "Every skill must exist in your curriculum.")
				return
			}
			checked[q.Skill] = true
		}
		v.Questions[i].ID = uuid.NewString()
	}
	v.ID = uuid.NewString()
	data, err := json.Marshal(v.Questions)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	_, err = s.DB.Exec(r.Context(), "INSERT INTO assessments(id,classroom_id,org_id,title,questions) VALUES($1,$2,$3,$4,$5)", v.ID, v.Classroom, platform.Actor(r).Org, v.Title, data)
	if err != nil {
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
	rows, err := s.DB.Query(r.Context(), "SELECT id,title FROM assessments WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", id, platform.Actor(r).Org)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	defer rows.Close()
	result := []map[string]string{}
	for rows.Next() {
		var id, title string
		if err = rows.Scan(&id, &title); err != nil {
			platform.Fail(w, err)
			return
		}
		result = append(result, map[string]string{"id": id, "title": title})
	}
	if rows.Err() != nil {
		platform.Fail(w, rows.Err())
		return
	}
	platform.JSON(w, 200, result)
}
func (s server) get(w http.ResponseWriter, r *http.Request) {
	v, err := s.load(r, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Assessment not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.Access(w, r, v.Classroom, "") {
		return
	}
	if platform.Actor(r).Has("teacher") {
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
	v, err := s.load(r, r.PathValue("id"))
	if err == pgx.ErrNoRows {
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
	tx, err := s.DB.Begin(r.Context())
	if err != nil {
		platform.Fail(w, err)
		return
	}
	defer tx.Rollback(r.Context())
	id := uuid.NewString()
	result, err := tx.Exec(r.Context(), "INSERT INTO submissions(id,assessment_id,student_id,answers) VALUES($1,$2,$3,$4) ON CONFLICT(assessment_id,student_id) DO NOTHING", id, v.ID, c.Subject, data)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if result.RowsAffected() == 0 {
		platform.Error(w, 409, "ALREADY_SUBMITTED", "This assessment has already been submitted.")
		return
	}
	err = platform.Emit(r.Context(), tx, "assessment.submitted.v1", id, map[string]any{"submission_id": id, "assessment_id": v.ID, "classroom_id": v.Classroom, "org_id": c.Org, "student_id": c.Subject, "questions": v.Questions, "answers": input.Answers})
	if err == nil {
		err = tx.Commit(r.Context())
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 201, map[string]string{"id": id, "status": "submitted"})
}
func main() {
	a, err := platform.New("assessment", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("POST /v1/assessments", s.create)
	a.Mux.HandleFunc("GET /v1/assessments", s.list)
	a.Mux.HandleFunc("GET /v1/assessments/{id}", s.get)
	a.Mux.HandleFunc("POST /v1/assessments/{id}/submissions", s.submit)
	if err = a.Run(a.Outbox); err != nil {
		os.Exit(1)
	}
}
