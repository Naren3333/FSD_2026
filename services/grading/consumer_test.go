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
	topicID      = "3f405162-7c8d-4e9f-8a1b-2c3d4e5f6071"
)

// submittedEvent is a submission event of the given version. Questions are written with topic_id; before v4 their
// topic_id is sent as skill_id, as those versions name it.
func submittedEvent(t *testing.T, topic string, data map[string]any) platform.Event {
	t.Helper()
	base := map[string]any{
		"submission_id": submissionID, "assessment_id": submissionID, "classroom_id": submissionID, "org_id": "org", "student_id": "student-1",
		"questions": []map[string]any{{"id": questionID, "prompt": "Why?", "type": "free_response", "options": nil, "answer": "", "tolerance": "", "points": 3, "topic_id": topicID,
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
	if topic != topicSubmittedV4 {
		body = legacyQuestions(t, body)
	}
	return platform.Event{Type: topic, Resource: submissionID, Data: body}
}

// legacyQuestions renames each question's topic_id to skill_id, as submissions before v4 carry it.
func legacyQuestions(t *testing.T, body []byte) []byte {
	t.Helper()
	var data map[string]any
	if err := json.Unmarshal(body, &data); err != nil {
		t.Fatal(err)
	}
	questions, _ := data["questions"].([]any)
	for _, q := range questions {
		if q, ok := q.(map[string]any); ok {
			if topic, ok := q["topic_id"]; ok {
				delete(q, "topic_id")
				q["skill_id"] = topic
			}
		}
	}
	body, err := json.Marshal(data)
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestEveryVersionCarriesTheQuestionsTopic(t *testing.T) {
	for _, topic := range []string{topicSubmittedV1, topicSubmittedV2, topicSubmittedV3, topicSubmittedV4} {
		v, ok := decodeSubmitted(submittedEvent(t, topic, nil))
		if !ok || v.Questions[0].Topic != topicID {
			t.Fatalf("%s: %v %+v", topic, ok, v.Questions)
		}
		evidence, err := v.score()
		if err != nil || evidence[0].Topic != topicID {
			t.Fatalf("%s: %+v %v", topic, evidence, err)
		}
	}
}

func TestEachVersionReadsOnlyItsOwnTopicField(t *testing.T) {
	other := "4a516273-8d9e-4f0a-9b1c-2d3e4f506172"
	question := func(name string) map[string]any {
		return map[string]any{"questions": []map[string]any{{"id": questionID, "prompt": "1+1?", "type": "numerical", "options": nil, "answer": "2", "tolerance": "0", "points": 1, name: other}}, "answers": map[string]string{questionID: "2"}}
	}
	// A v4 question naming its topic only in skill_id has none, and is an InvalidEvent.
	v, ok := decodeSubmitted(submittedEvent(t, topicSubmittedV4, question("skill_id")))
	if _, err := v.score(); !ok || v.Questions[0].Topic != "" || err == nil {
		t.Fatalf("v4 read skill_id: %v %+v %v", ok, v.Questions, err)
	}
	// Before v4 the topic is skill_id, whatever topic_id says.
	event := submittedEvent(t, topicSubmittedV3, question("topic_id"))
	var data map[string]any
	_ = json.Unmarshal(event.Data, &data)
	data["questions"].([]any)[0].(map[string]any)["topic_id"] = questionID
	event.Data, _ = json.Marshal(data)
	if v, ok = decodeSubmitted(event); !ok || v.Questions[0].Topic != other {
		t.Fatalf("v3 topic: %v %+v", ok, v.Questions)
	}
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

func TestOnlyV3AndV4SubmissionsCarryAMarkingScheme(t *testing.T) {
	questions := func(scheme any) map[string]any {
		event := submittedEvent(t, topicSubmittedV4, nil)
		var data map[string]any
		_ = json.Unmarshal(event.Data, &data)
		q := data["questions"].([]any)[0].(map[string]any)
		q["marking_scheme"] = scheme
		return map[string]any{"questions": []any{q}}
	}
	passages := []map[string]any{{"document_id": submissionID, "chunk_id": questionID, "page": 2, "text": "Full marks need the reason."}}
	for _, topic := range []string{topicSubmittedV3, topicSubmittedV4} {
		v, ok := decodeSubmitted(submittedEvent(t, topic, questions(passages)))
		if !ok || len(v.Questions[0].MarkingScheme) != 1 || v.Questions[0].MarkingScheme[0].Page != 2 {
			t.Fatalf("%s: %v %+v", topic, ok, v.Questions)
		}
	}
	if v, ok := decodeSubmitted(submittedEvent(t, topicSubmittedV2, questions(passages))); !ok || v.Questions[0].MarkingScheme != nil {
		t.Fatalf("a v2 event's passages were kept: %+v", v.Questions)
	}
	six := make([]map[string]any, maxExcerpts+1)
	for i := range six {
		six[i] = map[string]any{"text": "x"}
	}
	for name, scheme := range map[string]any{"blank": []map[string]any{{"text": " "}}, "too many": six} {
		if _, ok := decodeSubmitted(submittedEvent(t, topicSubmittedV4, questions(scheme))); ok {
			t.Errorf("%s passages accepted", name)
		}
	}
}

func TestAV1SubmissionGetsTodaysDefaults(t *testing.T) {
	questions := []map[string]any{{"id": questionID, "prompt": "1+1?", "type": "numerical", "options": nil, "answer": "2", "tolerance": "0", "topic_id": topicID}}
	event := submittedEvent(t, topicSubmittedV1, map[string]any{"questions": questions, "answers": map[string]string{questionID: "2"}, "late": nil, "release_policy": nil, "ai_marking": nil, "teacher_id": nil})
	v, ok := decodeSubmitted(event)
	if !ok || v.ReleasePolicy != releaseOnFinalize || v.marking() || v.Questions[0].Points != 1 {
		t.Fatalf("%v %+v", ok, v)
	}
}

func TestMalformedSubmissionsAreRefused(t *testing.T) {
	for name, change := range map[string]map[string]any{
		"unknown release": {"release_policy": "never"},
		"too many points": {"questions": []map[string]any{{"id": questionID, "prompt": "?", "type": "numerical", "options": nil, "answer": "1", "tolerance": "0", "points": 101, "topic_id": topicID}}},
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
		return map[string]any{"id": id, "prompt": "?", "type": kind, "options": nil, "answer": "1", "tolerance": "0", "points": points, "topic_id": topicID}
	}
	many := make([]map[string]any, 51)
	answers := map[string]string{}
	for i := range many {
		id := fmt.Sprintf("2e3f4051-6b7c-4d8e-9f0a-%012x", i)
		many[i], answers[id] = numerical(id, 1, "numerical"), "1"
	}
	cases := map[string]map[string]any{
		"repeated question": {"questions": []map[string]any{numerical(questionID, 1, "numerical"), numerical(questionID, 1, "numerical")}, "answers": map[string]string{questionID: "1", topicID: "1"}},
		"zero points":       {"questions": []map[string]any{numerical(questionID, 0, "numerical")}, "answers": map[string]string{questionID: "1"}},
		"too many":          {"questions": many, "answers": answers},
		"unknown type":      {"questions": []map[string]any{numerical(questionID, 1, "essay")}, "answers": map[string]string{questionID: "1"}},
		"answer elsewhere":  {"questions": []map[string]any{numerical(questionID, 1, "numerical")}, "answers": map[string]string{topicID: "1"}},
	}
	for name, change := range cases {
		var invalid platform.InvalidEvent
		// tx is nil: a malformed event must be refused before it touches the database.
		if err := consume(context.Background(), nil, submittedEvent(t, topicSubmittedV2, change)); !errors.As(err, &invalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
}
