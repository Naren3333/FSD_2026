package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

var errAlreadyFinalized = errors.New("evaluation already finalized")

// insertEvaluation runs inside the event consumer's transaction.
func insertEvaluation(ctx context.Context, tx pgx.Tx, v Submitted, evidence []byte) error {
	_, err := tx.Exec(ctx, "INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(id) DO NOTHING", v.Submission, v.Assessment, v.Classroom, v.Org, v.Student, evidence)
	return err
}

func (s server) listEvaluations(ctx context.Context, org, classroom string) ([]Evaluation, error) {
	rows, err := s.DB.Query(ctx, "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, org)
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

func (s server) findClassroom(ctx context.Context, org, id string) (string, error) {
	var classroom string
	err := s.DB.QueryRow(ctx, "SELECT classroom_id FROM evaluations WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom)
	return classroom, err
}

// finalizeEvaluation locks the evaluation row, applies the overrides, records
// the audit entry and emits the finalized event in one transaction. It returns
// errAlreadyFinalized, errScoreOutOfBounds or errUnknownQuestion for the
// business failures; any other error is an infrastructure failure.
func (s server) finalizeEvaluation(ctx context.Context, org, id, actor, reason string, overrides map[string]int) (Evaluation, error) {
	var v Evaluation
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return v, err
	}
	defer tx.Rollback(ctx)
	var before []byte
	err = tx.QueryRow(ctx, "SELECT id,assessment_id,classroom_id,student_id,status,evidence FROM evaluations WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org).Scan(&v.ID, &v.Assessment, &v.Classroom, &v.Student, &v.Status, &before)
	if err != nil {
		return v, err
	}
	if v.Status != "draft" {
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
	v.Status = "finalized"
	return v, nil
}
