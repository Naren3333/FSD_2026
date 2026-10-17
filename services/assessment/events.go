package main

import (
	"encoding/json"
	"time"
)

// maxSubmittedEvent is the largest submission event Assessment publishes, in bytes: below Kafka's default 1 MB
// message limit with room for the event envelope. The event repeats every question with its rubric and model
// answer, so a long assessment with long answers can pass it although each answer is within its own limit; such a
// submission is refused rather than accepted and never graded.
const maxSubmittedEvent = 900_000

// topicSubmitted carries each submission to Grading with the assessment's questions as they were submitted, answer
// keys and rubrics included, and the settings Grading acts on (contracts/events/assessment.submitted.v2.schema.json).
// v2 replaced v1 when questions gained points, short answers and free responses (ADR 0004).
const topicSubmitted = "assessment.submitted.v2"

// submittedEventTooLarge reports whether the submission's event would be over maxSubmittedEvent.
func submittedEventTooLarge(v Assessment, sub Submission) bool {
	body, err := json.Marshal(submittedEvent(v, sub))
	return err != nil || len(body) > maxSubmittedEvent
}

func submittedEvent(v Assessment, sub Submission) map[string]any {
	return map[string]any{
		"submission_id":  sub.ID,
		"assessment_id":  v.ID,
		"classroom_id":   v.Classroom,
		"org_id":         sub.Org,
		"student_id":     sub.Student,
		"questions":      v.Questions,
		"answers":        sub.Answers,
		"late":           sub.Late,
		"submitted_at":   sub.SubmittedAt.UTC().Format(time.RFC3339Nano),
		"release_policy": v.ReleasePolicy,
		"ai_marking":     v.AIMarking,
		"teacher_id":     v.CreatedBy,
	}
}
