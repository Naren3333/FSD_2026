package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

const skillID = "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"

func fixture() Assessment {
	v := Assessment{Classroom: "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b", Title: "Fractions", Questions: []Question{{ID: "q1", Prompt: "1 + 1?", Type: "numerical", Answer: "2", Tolerance: "0.01", Skill: skillID}}}
	v.normalize()
	return v
}

func essay() Question {
	return Question{ID: "q2", Prompt: "Why does the counter race?", Type: typeFreeResponse, Skill: skillID, Rubric: &Rubric{
		Criteria: []Criterion{
			{Title: "Names the race", Levels: []Level{{2, "Explains it"}, {0, "Missing"}, {1, "Mentions it"}}},
			{Title: "Fix", Levels: []Level{{0, "None"}, {3, "Uses a lock"}}},
		},
		ModelAnswer: "Two threads read and write the counter without a lock.",
	}}
}

func TestAnswerKeysNeverPublic(t *testing.T) {
	v := fixture()
	v.Questions = append(v.Questions, essay(), Question{ID: "q3", Prompt: "Capital?", Type: typeShortAnswer, AcceptedAnswers: []string{"Paris"}, Skill: skillID})
	v.normalize()
	b, err := json.Marshal(v.Public())
	if err != nil {
		t.Fatal(err)
	}
	for _, secret := range []string{`"answer"`, `"tolerance"`, `"rubric"`, `"accepted_answers"`, "Paris", "lock", "criteria"} {
		if strings.Contains(string(b), secret) {
			t.Fatalf("leaked %q: %s", secret, b)
		}
	}
}

func TestNumericValidation(t *testing.T) {
	v := fixture()
	if v.Validate() != nil {
		t.Fatal("valid assessment rejected")
	}
	v.Questions[0].Tolerance = "-0.1"
	if v.Validate() == nil {
		t.Fatal("negative tolerance accepted")
	}
	v.Questions[0].Tolerance = "NaN"
	if v.Validate() == nil {
		t.Fatal("NaN accepted")
	}
}

func TestDefaultsKeepTheOldBehaviour(t *testing.T) {
	v := fixture()
	if v.Status != statusPublished || v.LatePolicy != lateAccept || v.ReleasePolicy != releaseOnFinalize || v.Questions[0].Points != 1 {
		t.Fatalf("%+v", v)
	}
}

func TestAFreeResponseIsWorthItsRubric(t *testing.T) {
	v := fixture()
	v.Questions = []Question{essay()}
	v.Questions[0].Points = 50 // ignored: the rubric decides
	v.normalize()
	q := v.Questions[0]
	if err := v.Validate(); err != nil {
		t.Fatal(err)
	}
	if q.Points != 5 || q.MaxLength != defaultMaxLength || q.Rubric.Criteria[0].ID != "c1" || q.Rubric.Criteria[1].ID != "c2" {
		t.Fatalf("%+v", q)
	}
	if levels := q.Rubric.Criteria[0].Levels; levels[0].Points != 0 || levels[2].Points != 2 {
		t.Fatalf("levels not in order: %+v", levels)
	}
}

func TestInvalidAssessmentsAreRefused(t *testing.T) {
	past := time.Now().Add(-time.Hour)
	future := time.Now().Add(time.Hour)
	cases := map[string]func(v *Assessment){
		"due before opening":   func(v *Assessment) { v.OpensAt, v.DueAt = &future, &past },
		"unknown status":       func(v *Assessment) { v.Status = "archived" },
		"unknown late policy":  func(v *Assessment) { v.LatePolicy = "maybe" },
		"unknown release":      func(v *Assessment) { v.ReleasePolicy = "never" },
		"AI marking objective": func(v *Assessment) { v.AIMarking = true },
		"too many points":      func(v *Assessment) { v.Questions[0].Points = 101 },
		"rubric on numerical":  func(v *Assessment) { v.Questions[0].Rubric = essay().Rubric },
		"one rubric level": func(v *Assessment) {
			q := essay()
			q.Rubric.Criteria[1].Levels = q.Rubric.Criteria[1].Levels[:1]
			v.Questions = []Question{q}
		},
		"repeated level points": func(v *Assessment) {
			q := essay()
			q.Rubric.Criteria[1].Levels = []Level{{1, "a"}, {1, "b"}}
			v.Questions = []Question{q}
		},
		"no accepted answers": func(v *Assessment) {
			v.Questions = []Question{{ID: "q", Prompt: "?", Type: typeShortAnswer, Skill: skillID}}
		},
		"duplicate accepted answers": func(v *Assessment) {
			v.Questions = []Question{{ID: "q", Prompt: "?", Type: typeShortAnswer, AcceptedAnswers: []string{"Paris", " paris "}, Skill: skillID}}
		},
	}
	for name, change := range cases {
		v := fixture()
		change(&v)
		v.normalize()
		if v.Validate() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	v := fixture()
	v.DueAt = &past
	if v.publishable(time.Now()) == nil {
		t.Error("published with a due date already past")
	}
}

func TestOpeningAndDueDates(t *testing.T) {
	now := time.Now()
	later, earlier := now.Add(time.Hour), now.Add(-time.Hour)
	v := fixture()
	if !v.visibleToStudents(now) || v.late(now) || v.closed(now) {
		t.Fatal("an undated published assessment is open")
	}
	v.OpensAt = &later
	if v.visibleToStudents(now) {
		t.Fatal("students saw an assessment before it opened")
	}
	v.OpensAt, v.DueAt = nil, &earlier
	if !v.late(now) || v.closed(now) {
		t.Fatal("late work is accepted by default")
	}
	v.LatePolicy = lateReject
	if !v.closed(now) {
		t.Fatal("late work was accepted when refused")
	}
	v.Status = statusDraft
	if v.visibleToStudents(now) {
		t.Fatal("students saw a draft")
	}
}

func TestAnswers(t *testing.T) {
	v := fixture()
	for _, a := range []map[string]string{{}, {"other": "2"}, {"q1": "NaN"}, {"q1": "2", "other": "2"}} {
		if validateAnswers(v, a) {
			t.Fatal("invalid submission accepted")
		}
	}
	if !validateAnswers(v, map[string]string{"q1": "2.00"}) {
		t.Fatal("valid answer rejected")
	}
}

func TestSavedWorkMayBeIncompleteButNotForeign(t *testing.T) {
	v := fixture()
	q := essay()
	q.MaxLength = 10
	v.Questions = append(v.Questions, q)
	v.normalize()
	if !validateSaved(v, map[string]string{}) || !validateSaved(v, map[string]string{"q2": ""}) || !validateSaved(v, map[string]string{"q2": "ten chars!"}) {
		t.Fatal("incomplete work refused")
	}
	if validateSaved(v, map[string]string{"q9": "x"}) || validateSaved(v, map[string]string{"q2": "eleven char"}) {
		t.Fatal("foreign or overlong work saved")
	}
}

func TestAFreeResponsesPointsErrorNamesItsRubric(t *testing.T) {
	v := fixture()
	q := essay()
	for i := range q.Rubric.Criteria {
		q.Rubric.Criteria[i].Levels = []Level{{0, "none"}, {60, "all"}}
	}
	v.Questions = []Question{q}
	v.normalize()
	if err := v.Validate(); err == nil || !strings.Contains(err.Error(), "rubric") {
		t.Fatalf("a rubric worth %d points: %v", v.Questions[0].Points, err)
	}
	v.Questions[0].Rubric = nil
	if err := v.Validate(); err == nil || !strings.Contains(err.Error(), "rubric") {
		t.Fatalf("no rubric: %v", err)
	}
}

func TestTextLimitsCountCharactersNotBytes(t *testing.T) {
	v := fixture()
	v.Title = strings.Repeat("語", maxTitle)
	v.Questions[0].Prompt = strings.Repeat("語", maxPrompt)
	q := essay()
	q.Rubric.Criteria[0].Title = strings.Repeat("語", maxCriterion)
	q.Rubric.Criteria[0].Levels[0].Descriptor = strings.Repeat("語", maxDescriptor)
	v.Questions = append(v.Questions, q, Question{ID: "q3", Prompt: "?", Type: typeMultipleChoice, Options: []string{strings.Repeat("語", maxAnswer), "b"}, Answer: "b", Skill: skillID})
	v.normalize()
	if err := v.Validate(); err != nil {
		t.Fatalf("text at the limit in characters refused: %v", err)
	}
	v.Title += "語"
	if v.Validate() == nil {
		t.Fatal("a title past the limit accepted")
	}
}
