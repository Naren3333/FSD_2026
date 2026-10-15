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
	listAssessments(ctx context.Context, org, classroomID, student string) ([]Summary, error)
	listProgress(ctx context.Context, org, classroomID string) ([]Progress, error)
	insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, error)
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

// listProgress is the classroom's 100 newest assessments with how many students submitted each.
func (p pgStore) listProgress(ctx context.Context, org, classroomID string) ([]Progress, error) {
	rows, err := p.db.Query(ctx, "SELECT a.id,a.title,count(s.id) FROM assessments a LEFT JOIN submissions s ON s.assessment_id=a.id WHERE a.classroom_id=$1 AND a.org_id=$2 GROUP BY a.id ORDER BY a.created_at DESC LIMIT 100", classroomID, org)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Progress, error) {
		var v Progress
		err := row.Scan(&v.ID, &v.Title, &v.Submissions)
		return v, err
	})
}

// listAssessments is the classroom's 100 newest assessments, each with whether student has submitted it.
func (p pgStore) listAssessments(ctx context.Context, org, classroomID, student string) ([]Summary, error) {
	rows, err := p.db.Query(ctx, "SELECT a.id,a.title,EXISTS(SELECT 1 FROM submissions s WHERE s.assessment_id=a.id AND s.student_id=$3) FROM assessments a WHERE a.classroom_id=$1 AND a.org_id=$2 ORDER BY a.created_at DESC LIMIT 100", classroomID, org, student)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Summary{}
	for rows.Next() {
		var v Summary
		if err = rows.Scan(&v.ID, &v.Title, &v.Submitted); err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

// insertSubmission stores the submission and emits the outbox event in one
// transaction. It reports inserted=false (with a nil error) when the student
// has already submitted this assessment.
func (p pgStore) insertSubmission(ctx context.Context, v Assessment, sub Submission) (bool, error) {
	answers, err := json.Marshal(sub.Answers)
	if err != nil {
		return false, err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	result, err := tx.Exec(ctx, "INSERT INTO submissions(id,assessment_id,student_id,answers) VALUES($1,$2,$3,$4) ON CONFLICT(assessment_id,student_id) DO NOTHING", sub.ID, v.ID, sub.Student, answers)
	if err != nil {
		return false, err
	}
	if result.RowsAffected() == 0 {
		return false, nil
	}
	err = platform.Emit(ctx, tx, "assessment.submitted.v1", sub.ID, map[string]any{"submission_id": sub.ID, "assessment_id": v.ID, "classroom_id": v.Classroom, "org_id": sub.Org, "student_id": sub.Student, "questions": v.Questions, "answers": sub.Answers})
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
