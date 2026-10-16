package main

import "time"

// topicSubmitted carries each submission to Grading with the assessment's questions as they were submitted, answer
// keys and rubrics included, and the settings Grading acts on (contracts/events/assessment.submitted.v2.schema.json).
// v2 replaced v1 when questions gained points, short answers and free responses (ADR 0004).
const topicSubmitted = "assessment.submitted.v2"

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
