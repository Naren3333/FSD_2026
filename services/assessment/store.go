package main

import (
	"context"
	"encoding/json"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is a classroom's assessments and its students' submissions.
type store interface {
	findAssessment(ctx context.Context, org, id string) (Assessment, error)
	insertAssessment(ctx context.Context, org string, v Assessment) error
	listAssessments(ctx context.Context, org, classroomID string) ([]map[string]string, error)
	insertSubmission(ctx context.Context, id string, v Assessment, org, studentID string, data []byte, answers map[string]string) (bool, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

func (p pgStore) findAssessment(ctx context.Context, org, id string) (Assessment, error) {
	var v Assessment
	if !platform.ID(id) {
		return v, pgx.ErrNoRows
	}
	var data []byte
	err := p.db.QueryRow(ctx, "SELECT id,classroom_id,title,questions FROM assessments WHERE id=$1 AND org_id=$2", id, org).Scan(&v.ID, &v.Classroom, &v.Title, &data)
	if err == nil {
		err = json.Unmarshal(data, &v.Questions)
	}
	return v, err
}

func (p pgStore) insertAssessment(ctx context.Context, org string, v Assessment) error {
	data, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	_, err = p.db.Exec(ctx, "INSERT INTO assessments(id,classroom_id,org_id,title,questions) VALUES($1,$2,$3,$4,$5)", v.ID, v.Classroom, org, v.Title, data)
	return err
}

func (p pgStore) listAssessments(ctx context.Context, org, classroomID string) ([]map[string]string, error) {
	rows, err := p.db.Query(ctx, "SELECT id,title FROM assessments WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroomID, org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []map[string]string{}
	for rows.Next() {
		var id, title string
		if err = rows.Scan(&id, &title); err != nil {
			return nil, err
		}
		result = append(result, map[string]string{"id": id, "title": title})
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

// insertSubmission stores the submission and emits the outbox event in one
// transaction. It reports inserted=false (with a nil error) when the student
// has already submitted this assessment.
func (p pgStore) insertSubmission(ctx context.Context, id string, v Assessment, org, studentID string, data []byte, answers map[string]string) (bool, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	result, err := tx.Exec(ctx, "INSERT INTO submissions(id,assessment_id,student_id,answers) VALUES($1,$2,$3,$4) ON CONFLICT(assessment_id,student_id) DO NOTHING", id, v.ID, studentID, data)
	if err != nil {
		return false, err
	}
	if result.RowsAffected() == 0 {
		return false, nil
	}
	err = platform.Emit(ctx, tx, "assessment.submitted.v1", id, map[string]any{"submission_id": id, "assessment_id": v.ID, "classroom_id": v.Classroom, "org_id": org, "student_id": studentID, "questions": v.Questions, "answers": answers})
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
