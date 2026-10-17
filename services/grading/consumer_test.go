package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"learning/platform"
	"testing"
)

const (
	submissionID = "1d2e3f40-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
	questionID   = "2e3f4051-6b7c-4d8e-9f0a-1b2c3d4e5f60"
	skillID      = "3f405162-7c8d-4e9f-8a1b-2c3d4e5f6071"
)

func submittedEvent(t *testing.T, topic string, data map[string]any) platform.Event {
	t.Helper()
	base := map[string]any{
		"submission_id": submissionID, "assessment_id": submissionID, "classroom_id": submissionID, "org_id": "org", "student_id": "student-1",
		"questions": []map[string]any{{"id": questionID, "prompt": "Why?", "type": "free_response", "options": nil, "answer": "", "tolerance": "", "points": 3, "skill_id": skillID,
			"rubric": map[string]any{"criteria": []map[string]any{{"id": "c1", "title": "Reason", "levels": []map[string]any{{"points": 0, "descriptor": "None"}, {"points": 3, "descriptor": "Full"}}}}}}},
		"answers": map[string]string{questionID: "Because."}, "late": true, "release_policy": "manual", "ai_marking": true, "teacher_id": "teacher-1",
	}
	for k, v := range data {
		base[k] = v
	}
	body, err := json.Marshal(base)
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: topic, Resource: submissionID, Data: body}
}

func TestAV2SubmissionKeepsItsSettings(t *testing.T) {
	v, ok := decodeSubmitted(submittedEvent(t, topicSubmittedV2, nil))
	if !ok || !v.Late || v.ReleasePolicy != releaseManual || !v.marking() || v.Questions[0].Points != 3 {
		t.Fatalf("%v %+v", ok, v)
	}
	evidence, err := v.score()
	if err != nil || !evidence[0].NeedsMarking || evidence[0].Possible != 3 {
		t.Fatalf("%+v %v", evidence, err)
	}
}

func TestAV1SubmissionGetsTodaysDefaults(t *testing.T) {
	questions := []map[string]any{{"id": questionID, "prompt": "1+1?", "type": "numerical", "options": nil, "answer": "2", "tolerance": "0", "skill_id": skillID}}
	event := submittedEvent(t, topicSubmittedV1, map[string]any{"questions": questions, "answers": map[string]string{questionID: "2"}, "late": nil, "release_policy": nil, "ai_marking": nil, "teacher_id": nil})
	v, ok := decodeSubmitted(event)
	if !ok || v.ReleasePolicy != releaseOnFinalize || v.marking() || v.Questions[0].Points != 1 {
		t.Fatalf("%v %+v", ok, v)
	}
}

func TestMalformedSubmissionsAreRefused(t *testing.T) {
	for name, change := range map[string]map[string]any{
		"unknown release": {"release_policy": "never"},
		"too many points": {"questions": []map[string]any{{"id": questionID, "prompt": "?", "type": "numerical", "options": nil, "answer": "1", "tolerance": "0", "points": 101, "skill_id": skillID}}},
		"missing answer":  {"answers": map[string]string{}},
	} {
		if _, ok := decodeSubmitted(submittedEvent(t, topicSubmittedV2, change)); ok {
			t.Errorf("%s: accepted", name)
		}
	}
	event := submittedEvent(t, topicSubmittedV2, nil)
	event.Resource = questionID
	if _, ok := decodeSubmitted(event); ok {
		t.Error("accepted an event about another submission")
	}
}

func TestOnlyANamedTeacherPaysForSuggestions(t *testing.T) {
	v, _ := decodeSubmitted(submittedEvent(t, topicSubmittedV2, map[string]any{"teacher_id": ""}))
	if v.marking() {
		t.Fatal("queued AI marking with nobody to bill")
	}
}

func TestMalformedQuestionsAreInvalidEvents(t *testing.T) {
	numerical := func(id string, points int, kind string) map[string]any {
		return map[string]any{"id": id, "prompt": "?", "type": kind, "options": nil, "answer": "1", "tolerance": "0", "points": points, "skill_id": skillID}
	}
	many := make([]map[string]any, 51)
	answers := map[string]string{}
	for i := range many {
		id := fmt.Sprintf("2e3f4051-6b7c-4d8e-9f0a-%012x", i)
		many[i], answers[id] = numerical(id, 1, "numerical"), "1"
	}
	cases := map[string]map[string]any{
		"repeated question": {"questions": []map[string]any{numerical(questionID, 1, "numerical"), numerical(questionID, 1, "numerical")}, "answers": map[string]string{questionID: "1", skillID: "1"}},
		"zero points":       {"questions": []map[string]any{numerical(questionID, 0, "numerical")}, "answers": map[string]string{questionID: "1"}},
		"too many":          {"questions": many, "answers": answers},
		"unknown type":      {"questions": []map[string]any{numerical(questionID, 1, "essay")}, "answers": map[string]string{questionID: "1"}},
		"answer elsewhere":  {"questions": []map[string]any{numerical(questionID, 1, "numerical")}, "answers": map[string]string{skillID: "1"}},
	}
	for name, change := range cases {
		var invalid platform.InvalidEvent
		// tx is nil: a malformed event must be refused before it touches the database.
		if err := consume(context.Background(), nil, submittedEvent(t, topicSubmittedV2, change)); !errors.As(err, &invalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
}
