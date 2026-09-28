package main

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"sync/atomic"
)

//go:embed migrations/001_init.sql
var migration string

type Submitted struct {
	Submission string            `json:"submission_id"`
	Assessment string            `json:"assessment_id"`
	Classroom  string            `json:"classroom_id"`
	Org        string            `json:"org_id"`
	Student    string            `json:"student_id"`
	Questions  []Question        `json:"questions"`
	Answers    map[string]string `json:"answers"`
}

func consume(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	var v Submitted
	if json.Unmarshal(event.Data, &v) != nil || event.Resource != v.Submission || !platform.ID(v.Submission) || !platform.ID(v.Assessment) || !platform.ID(v.Classroom) || v.Student == "" || v.Org == "" || len(v.Questions) == 0 || len(v.Questions) > 50 || len(v.Answers) != len(v.Questions) {
		return platform.InvalidEvent{Reason: "invalid submission"}
	}
	evidence := []Evidence{}
	seen := map[string]bool{}
	for _, q := range v.Questions {
		answer, exists := v.Answers[q.ID]
		if !exists || seen[q.ID] || !platform.ID(q.ID) || !platform.ID(q.Skill) {
			return platform.InvalidEvent{Reason: "invalid question references"}
		}
		seen[q.ID] = true
		e, err := evaluate(q, answer)
		if err != nil {
			return platform.InvalidEvent{Reason: err.Error()}
		}
		evidence = append(evidence, e)
	}
	data, err := json.Marshal(evidence)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO NOTHING", v.Submission, v.Assessment, v.Classroom, v.Org, v.Student, data)
	return err
}

type Evaluation struct {
	ID         string     `json:"id"`
	Assessment string     `json:"assessment_id"`
	Classroom  string     `json:"classroom_id"`
	Student    string     `json:"student_id"`
	Status     string     `json:"status"`
	Evidence   []Evidence `json:"evidence"`
}

func main() {
	a, err := platform.New("grading", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	a.Mux.HandleFunc("GET /v1/evaluations", func(w http.ResponseWriter, r *http.Request) {
		if !platform.Teacher(w, r) {
			return
		}
		classroom := r.URL.Query().Get("classroom_id")
		if !a.Access(w, r, classroom, "") {
			return
		}
		rows, err := a.DB.Query(r.Context(), "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, platform.Actor(r).Org)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer rows.Close()
		result := []Evaluation{}
		for rows.Next() {
			var v Evaluation
			var data []byte
			if err = rows.Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &data); err != nil {
				platform.Fail(w, err)
				return
			}
			if err = json.Unmarshal(data, &v.Evidence); err != nil {
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
	})
	a.Mux.HandleFunc("POST /v1/evaluations/{id}/finalize", func(w http.ResponseWriter, r *http.Request) {
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
		var classroom string
		err := a.DB.QueryRow(r.Context(), "SELECT classroom_id FROM evaluations WHERE id=$1 AND org_id=$2", r.PathValue("id"), platform.Actor(r).Org).Scan(&classroom)
		if err == pgx.ErrNoRows {
			platform.Error(w, 404, "NOT_FOUND", "Evaluation not found.")
			return
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if !a.Access(w, r, classroom, "") {
			return
		}
		tx, err := a.DB.Begin(r.Context())
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer tx.Rollback(r.Context())
		var v Evaluation
		var before []byte
		err = tx.QueryRow(r.Context(), "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE id=$1 AND org_id=$2 FOR UPDATE", r.PathValue("id"), platform.Actor(r).Org).Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &before)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if v.Status != "draft" {
			platform.Error(w, 409, "ALREADY_FINALIZED", "This evaluation has already been finalized.")
			return
		}
		if err = json.Unmarshal(before, &v.Evidence); err != nil {
			platform.Fail(w, err)
			return
		}
		applied := 0
		for i, e := range v.Evidence {
			if score, ok := input.Overrides[e.Question]; ok {
				if score < 0 || score > e.Possible {
					platform.Error(w, 422, "INVALID_OVERRIDE", "Scores must be within question bounds.")
					return
				}
				v.Evidence[i].Awarded = score
				v.Evidence[i].Correct = score == e.Possible
				applied++
			}
		}
		if applied != len(input.Overrides) {
			platform.Error(w, 422, "INVALID_OVERRIDE", "Override references an unknown question.")
			return
		}
		after, err := json.Marshal(v.Evidence)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		_, err = tx.Exec(r.Context(), "UPDATE evaluations SET evidence=$1,status='finalized',finalized_by=$2,finalized_at=now() WHERE id=$3", after, platform.Actor(r).Subject, v.ID)
		if err == nil {
			_, err = tx.Exec(r.Context(), "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'finalize',$3,$4,$5)", v.ID, platform.Actor(r).Subject, input.Reason, before, after)
		}
		if err == nil {
			err = platform.Emit(r.Context(), tx, "grading.finalized.v1", v.ID, map[string]any{"evaluation_id": v.ID, "assessment_id": v.Assessment, "classroom_id": v.Classroom, "org_id": platform.Actor(r).Org, "student_id": v.Student, "evidence": v.Evidence})
		}
		if err == nil {
			err = tx.Commit(r.Context())
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		v.Status = "finalized"
		platform.JSON(w, 200, v)
	})
	var consumerRunning atomic.Bool
	a.Checks = append(a.Checks, func(ctx context.Context) error {
		if !consumerRunning.Load() {
			return errors.New("consumer is stopped")
		}
		return platform.KafkaReady(ctx)
	})
	if err = a.Run(a.Outbox, func(ctx context.Context) {
		consumerRunning.Store(true)
		defer consumerRunning.Store(false)
		a.Consume(ctx, "assessment.submitted.v1", consume)
	}); err != nil {
		os.Exit(1)
	}
}
