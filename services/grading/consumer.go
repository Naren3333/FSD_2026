package main

import (
	"context"
	"encoding/json"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

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
	return insertEvaluation(ctx, tx, v, data)
}
