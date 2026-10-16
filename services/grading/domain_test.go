package main

import (
	"errors"
	"testing"
)

func TestExactDecimalGrading(t *testing.T) {
	for _, v := range []struct {
		answer, key, tolerance string
		want                   bool
	}{{"0.3", "0.30", "0", true}, {"1.01", "1", "0.01", true}, {"1.01000001", "1", "0.01", false}, {"-2.5", "-2.50", "0", true}} {
		got, err := evaluate(Question{Type: "numerical", Answer: v.key, Tolerance: v.tolerance}, v.answer)
		if err != nil || got.Correct != v.want {
			t.Fatalf("%+v: %+v %v", v, got, err)
		}
	}
}

func TestInvalidValues(t *testing.T) {
	for _, v := range []string{"NaN", "Inf", "1e2", "", "1/2"} {
		if _, err := evaluate(Question{Type: "numerical", Answer: "1", Tolerance: "0"}, v); err == nil {
			t.Fatalf("accepted %s", v)
		}
	}
}

func TestObjectiveScore(t *testing.T) {
	got, err := evaluate(Question{Type: "multiple_choice", Answer: "B"}, "A")
	if err != nil || got.Awarded != 0 || got.Possible != 1 {
		t.Fatal("wrong score")
	}
}

func rubricQuestion() Question {
	return Question{ID: "q2", Type: typeFreeResponse, Points: 5, Rubric: &Rubric{Criteria: []Criterion{
		{ID: "c1", Levels: []Level{{0, "Missing"}, {1, "Mentions"}, {2, "Explains"}}},
		{ID: "c2", Levels: []Level{{0, "None"}, {3, "Uses a lock"}}},
	}}}
}

func markable() ([]Evidence, []Question) {
	questions := []Question{{ID: "q1", Type: typeNumerical, Points: 2}, rubricQuestion()}
	evidence := []Evidence{
		{Question: "q1", Possible: 2, Awarded: 0, MarkedBy: markedAuto},
		{Question: "q2", Possible: 5, NeedsMarking: true},
	}
	return evidence, questions
}

func points(n int) *int { return &n }

func text(s string) *string { return &s }

func TestTheTeacherMarksAFreeResponseByItsRubric(t *testing.T) {
	evidence, questions := markable()
	if !unmarked(evidence) {
		t.Fatal("an unmarked free response did not hold up finalization")
	}
	err := applyMarks(evidence, questions, map[string]Mark{"q2": {Criteria: map[string]int{"c1": 2, "c2": 0}, Feedback: text("Name the fix.")}})
	e := evidence[1]
	if err != nil || e.Awarded != 2 || e.Correct || e.NeedsMarking || e.MarkedBy != markedTeacher || e.Feedback != "Name the fix." || len(e.Criteria) != 2 {
		t.Fatalf("%+v %v", e, err)
	}
	if unmarked(evidence) {
		t.Fatal("still unmarked")
	}
	if err = applyMarks(evidence, questions, map[string]Mark{"q2": {Criteria: map[string]int{"c1": 2, "c2": 3}}}); err != nil || !evidence[1].Correct || evidence[1].Awarded != 5 {
		t.Fatalf("full marks: %+v %v", evidence[1], err)
	}
}

func TestMarksMustFitTheirQuestion(t *testing.T) {
	cases := map[string]struct {
		marks map[string]Mark
		want  error
	}{
		"unknown question":         {map[string]Mark{"q9": {Awarded: points(1)}}, errUnknownQuestion},
		"too many points":          {map[string]Mark{"q1": {Awarded: points(3)}}, errScoreOutOfBounds},
		"negative points":          {map[string]Mark{"q1": {Awarded: points(-1)}}, errScoreOutOfBounds},
		"points on a rubric":       {map[string]Mark{"q2": {Awarded: points(5)}}, errInvalidCriteria},
		"criteria on numerical":    {map[string]Mark{"q1": {Criteria: map[string]int{"c1": 1}}}, errInvalidCriteria},
		"a missing criterion":      {map[string]Mark{"q2": {Criteria: map[string]int{"c1": 1}}}, errInvalidCriteria},
		"a level that isn't there": {map[string]Mark{"q2": {Criteria: map[string]int{"c1": 1, "c2": 2}}}, errInvalidCriteria},
		"an extra criterion":       {map[string]Mark{"q2": {Criteria: map[string]int{"c1": 1, "c2": 0, "c3": 0}}}, errInvalidCriteria},
	}
	for name, tc := range cases {
		evidence, questions := markable()
		if err := applyMarks(evidence, questions, tc.marks); !errors.Is(err, tc.want) {
			t.Errorf("%s: got %v", name, err)
		}
	}
}

func TestFeedbackAloneKeepsTheScore(t *testing.T) {
	evidence, questions := markable()
	if err := applyMarks(evidence, questions, map[string]Mark{"q2": {Feedback: text("Keep going.")}}); err != nil || !evidence[1].NeedsMarking || evidence[1].Feedback != "Keep going." {
		t.Fatalf("%+v %v", evidence[1], err)
	}
}

func TestOldOverridesStillWork(t *testing.T) {
	evidence, questions := markable()
	marks := overridesAsMarks(map[string]int{"q1": 2}, map[string]Mark{"q2": {Criteria: map[string]int{"c1": 0, "c2": 0}}})
	if err := applyMarks(evidence, questions, marks); err != nil || !evidence[0].Correct || evidence[0].Awarded != 2 || evidence[0].MarkedBy != markedTeacher {
		t.Fatalf("%+v %v", evidence[0], err)
	}
}

func TestStudentsSeeMarksAndFeedbackOnly(t *testing.T) {
	v := Evaluation{Evidence: []Evidence{{Question: "q", Awarded: 1, Possible: 2, Feedback: "Nearly.", Criteria: []CriterionScore{{"c1", 1}}, Suggestion: &Suggestion{Feedback: "AI"}}}}
	own := v.own()
	if own.Evidence[0] != (OwnEvidence{Question: "q", Awarded: 1, Possible: 2, Feedback: "Nearly."}) {
		t.Fatalf("%+v", own.Evidence[0])
	}
}
