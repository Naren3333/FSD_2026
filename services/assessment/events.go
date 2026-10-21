package main

import (
	"encoding/json"
	"time"
)

// maxsubmittedevent is the largest submission event assessment publishes, in bytes: below kafka's default 1
// mb message limit with room for the event envelope.
const maxSubmittedEvent = 900_000

// publish submitted questions, answer keys and marking guidance for grading.
const topicSubmitted = "assessment.submitted.v4"

// topicsubmittedall matches every submission version's topic, for removing a deleted assessment's events
// from the outbox whichever version they were written as.
const topicSubmittedAll = "assessment.submitted.%"

// announce deleted assessments so grading and analytics purge their copies.
const topicDeleted = "assessment.deleted.v1"

// topicduechanged announces a published assessment's new due date, or its removal (null)
// (contracts/events/assessment.due_changed.v1.schema.json): grading sets its evaluations' late flags
// against it.
const topicDueChanged = "assessment.due_changed.v1"

// submittedeventtoolarge reports whether the submission's event would be over maxsubmittedevent.
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
		"questions":      eventQuestions(v.Questions),
		"answers":        sub.Answers,
		"late":           sub.Late,
		"submitted_at":   sub.SubmittedAt.UTC().Format(time.RFC3339Nano),
		"release_policy": v.ReleasePolicy,
		"ai_marking":     v.AIMarking,
		"teacher_id":     v.CreatedBy,
	}
}

// eventquestions are the questions as grading gets them: without where a generated question came from,
// which only the teacher's editor uses (adr 0006) and the v4 contract does not carry.
func eventQuestions(questions []Question) []Question {
	out := make([]Question, len(questions))
	for i, q := range questions {
		q.Sources, q.Origin = nil, ""
		out[i] = q
	}
	return out
}
