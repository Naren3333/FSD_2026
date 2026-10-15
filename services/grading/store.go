package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var errAlreadyFinalized = errors.New("evaluation already finalized")

// store is a classroom's draft and finalized evaluations, with their audit trail.
type store interface {
	listEvaluations(ctx context.Context, org, classroom string) ([]Evaluation, error)
	listFinalized(ctx context.Context, org, classroom, student string) ([]Evaluation, error)
	countEvaluations(ctx context.Context, org, classroom string) (Counts, error)
	findClassroom(ctx context.Context, org, id string) (string, error)
	finalizeEvaluation(ctx context.Context, org, id, actor, reason string, overrides map[string]int) (Evaluation, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

// Counts is how many of a classroom's evaluations are drafts waiting for its teacher, and how many are finalized.
type Counts struct {
	Drafts    int `json:"drafts"`
	Finalized int `json:"finalized"`
}

func (p pgStore) countEvaluations(ctx context.Context, org, classroom string) (Counts, error) {
	var c Counts
	err := p.db.QueryRow(ctx, "SELECT count(*) FILTER (WHERE status='draft'),count(*) FILTER (WHERE status='finalized') FROM evaluations WHERE classroom_id=$1 AND org_id=$2", classroom, org).Scan(&c.Drafts, &c.Finalized)
	return c, err
}

// insertEvaluation runs inside the event consumer's transaction.
func insertEvaluation(ctx context.Context, tx pgx.Tx, v Submitted, evidence []byte) error {
	_, err := tx.Exec(ctx, "INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence) SELECT $1::uuid,$2::uuid,$3::uuid,$4::text,$5::text,$6::jsonb WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$3) ON CONFLICT(id) DO NOTHING", v.Submission, v.Assessment, v.Classroom, v.Org, v.Student, evidence)
	return err
}

func (p pgStore) listEvaluations(ctx context.Context, org, classroom string) ([]Evaluation, error) {
	rows, err := p.db.Query(ctx, "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Evaluation{}
	for rows.Next() {
		var v Evaluation
		var data []byte
		if err = rows.Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &data); err != nil {
			return nil, err
		}
		if err = json.Unmarshal(data, &v.Evidence); err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

// listFinalized is one student's finalized results in a classroom, newest first. Drafts never leave Grading for
// students: a grade is the student's only once a teacher has finalized it.
func (p pgStore) listFinalized(ctx context.Context, org, classroom, student string) ([]Evaluation, error) {
	rows, err := p.db.Query(ctx, "SELECT id,assessment_id,classroom_id,student_id,status,evidence,finalized_at FROM evaluations WHERE classroom_id=$1 AND org_id=$2 AND student_id=$3 AND status='finalized' ORDER BY finalized_at DESC LIMIT 100", classroom, org, student)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Evaluation, error) {
		var v Evaluation
		var data []byte
		if err := row.Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &data, &v.FinalizedAt); err != nil {
			return v, err
		}
		return v, json.Unmarshal(data, &v.Evidence)
	})
}

func (p pgStore) findClassroom(ctx context.Context, org, id string) (string, error) {
	var classroom string
	err := p.db.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom)
	return classroom, err
}

// finalizeEvaluation locks the evaluation row, applies the overrides, records
// the audit entry and emits the finalized event in one transaction. It returns
// errAlreadyFinalized, errScoreOutOfBounds or errUnknownQuestion for the
// business failures; any other error is an infrastructure failure.
func (p pgStore) finalizeEvaluation(ctx context.Context, org, id, actor, reason string, overrides map[string]int) (Evaluation, error) {
	var v Evaluation
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return v, err
	}
	defer tx.Rollback(ctx)
	var before []byte
	err = tx.QueryRow(ctx, "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org).Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &before)
	if err != nil {
		return v, err
	}
	if v.Status != statusDraft {
		return v, errAlreadyFinalized
	}
	if err = json.Unmarshal(before, &v.Evidence); err != nil {
		return v, err
	}
	if err = applyOverrides(v.Evidence, overrides); err != nil {
		return v, err
	}
	after, err := json.Marshal(v.Evidence)
	if err != nil {
		return v, err
	}
	_, err = tx.Exec(ctx, "UPDATE evaluations SET evidence=$1,status='finalized',finalized_by=$2,finalized_at=now() WHERE id=$3", after, actor, v.ID)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,$2,'finalize',$3,$4,$5)", v.ID, actor, reason, before, after)
	}
	if err == nil {
		err = platform.Emit(ctx, tx, "grading.finalized.v1", v.ID, map[string]any{"evaluation_id": v.ID, "assessment_id": v.Assessment, "classroom_id": v.Classroom, "org_id": org, "student_id": v.Student, "evidence": v.Evidence})
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return v, err
	}
	v.Status = statusFinalized
	return v, nil
}
