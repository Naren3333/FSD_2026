package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
)

// Submission topics. v2 carries points, short answers, free responses and the assessment's grading settings; v1
// events still on Kafka from before v2 are read as v2 with today's defaults (one point a question, results
// released on finalization, no AI marking).
const (
	topicSubmittedV1 = "assessment.submitted.v1"
	topicSubmittedV2 = "assessment.submitted.v2"
	topicFinalized   = "grading.finalized.v2"
)

type Submitted struct {
	Submission    string            `json:"submission_id"`
	Assessment    string            `json:"assessment_id"`
	Classroom     string            `json:"classroom_id"`
	Org           string            `json:"org_id"`
	Student       string            `json:"student_id"`
	Questions     []Question        `json:"questions"`
	Answers       map[string]string `json:"answers"`
	Late          bool              `json:"late"`
	SubmittedAt   *time.Time        `json:"submitted_at"`
	ReleasePolicy string            `json:"release_policy"`
	AIMarking     bool              `json:"ai_marking"`
	Teacher       string            `json:"teacher_id"`
}

func (v Submitted) valid() bool {
	if v.ReleasePolicy != releaseOnFinalize && v.ReleasePolicy != releaseManual {
		return false
	}
	for _, q := range v.Questions {
		if q.Points < 1 || q.Points > 100 {
			return false
		}
	}
	return platform.ID(v.Submission) && platform.ID(v.Assessment) && platform.ID(v.Classroom) && v.Student != "" && v.Org != "" &&
		len(v.Questions) > 0 && len(v.Questions) <= 50 && len(v.Answers) == len(v.Questions)
}

// decodeSubmitted reads either submission version, filling in what v1 lacks.
func decodeSubmitted(event platform.Event) (Submitted, bool) {
	var v Submitted
	if json.Unmarshal(event.Data, &v) != nil || event.Resource != v.Submission {
		return v, false
	}
	if event.Type == topicSubmittedV1 {
		v.ReleasePolicy, v.AIMarking, v.Teacher, v.Late = releaseOnFinalize, false, "", false
		for i := range v.Questions {
			v.Questions[i].Points = 1
		}
	}
	return v, v.valid()
}

// score evaluates each answer against its question; a question without exactly one answer is an InvalidEvent.
func (v Submitted) score() ([]Evidence, error) {
	evidence := make([]Evidence, 0, len(v.Questions))
	seen := map[string]bool{}
	for _, q := range v.Questions {
		answer, exists := v.Answers[q.ID]
		if !exists || seen[q.ID] || !platform.ID(q.ID) || !platform.ID(q.Skill) {
			return nil, platform.InvalidEvent{Reason: "invalid question references"}
		}
		seen[q.ID] = true
		e, err := evaluate(q, answer)
		if err != nil {
			return nil, platform.InvalidEvent{Reason: err.Error()}
		}
		evidence = append(evidence, e)
	}
	return evidence, nil
}

// marking reports whether the submission's unmarked questions get AI suggestions: the assessment asks for them and
// names the teacher whose allowance pays (ADR 0003).
func (v Submitted) marking() bool { return v.AIMarking && v.Teacher != "" }

// consume scores a submitted assessment into a draft evaluation, queueing an AI suggestion for each question that
// needs the teacher when the assessment asks for them. A malformed submission is an InvalidEvent.
func consume(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, ok := decodeSubmitted(event)
	if !ok {
		return platform.InvalidEvent{Reason: "invalid submission"}
	}
	evidence, err := v.score()
	if err != nil {
		return err
	}
	var queued []string
	if v.marking() {
		for i := range evidence {
			if evidence[i].NeedsMarking {
				evidence[i].Suggestion = &Suggestion{Status: suggestionPending}
				queued = append(queued, evidence[i].Question)
			}
		}
	}
	return insertEvaluation(ctx, tx, v, evidence, queued)
}
