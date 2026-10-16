package main

import "testing"

func TestNumericalAnswersAreExactWithinTheTolerance(t *testing.T) {
	q := Question{Type: typeNumerical, Answer: "0.3", Tolerance: "0.05"}
	for answer, want := range map[string]bool{"0.30": true, "0.35": true, "0.351": false, "0.25": true, "0.2": false} {
		got, _, err := numerical{}.mark(q, answer)
		if err != nil || got != want {
			t.Errorf("%s: got %v, %v", answer, got, err)
		}
	}
	if _, _, err := (numerical{}).mark(q, "about 0.3"); err == nil {
		t.Fatal("scored a non-decimal answer")
	}
}

func TestAnUnknownTypeCannotBeScored(t *testing.T) {
	if _, err := evaluate(Question{ID: "q1", Type: "essay"}, "x"); err == nil {
		t.Fatal("scored an essay question")
	}
}

func TestAShortAnswerIsRightOrGoesToTheTeacher(t *testing.T) {
	q := Question{ID: "q", Type: typeShortAnswer, AcceptedAnswers: []string{"New York", "NYC"}, Points: 2}
	right, err := evaluate(q, "  new   york ")
	if err != nil || !right.Correct || right.Awarded != 2 || right.Possible != 2 || right.NeedsMarking || right.MarkedBy != markedAuto {
		t.Fatalf("%+v %v", right, err)
	}
	other, err := evaluate(q, "The Big Apple")
	if err != nil || other.Correct || other.Awarded != 0 || !other.NeedsMarking || other.MarkedBy != "" {
		t.Fatalf("an unmatched short answer was marked wrong: %+v %v", other, err)
	}
	q.CaseSensitive = true
	if e, _ := evaluate(q, "nyc"); !e.NeedsMarking {
		t.Fatal("case was ignored when it matters")
	}
}

func TestAFreeResponseAlwaysGoesToTheTeacher(t *testing.T) {
	q := Question{ID: "q", Type: typeFreeResponse, Points: 5, Rubric: &Rubric{Criteria: []Criterion{{ID: "c1"}}}}
	e, err := evaluate(q, "An essay.")
	if err != nil || !e.NeedsMarking || e.Possible != 5 || e.Awarded != 0 {
		t.Fatalf("%+v %v", e, err)
	}
	if _, err = evaluate(Question{ID: "q", Type: typeFreeResponse, Points: 5}, "x"); err == nil {
		t.Fatal("marked a free response without a rubric")
	}
}

func TestObjectiveQuestionsEarnTheirPoints(t *testing.T) {
	e, err := evaluate(Question{Type: typeMultipleChoice, Answer: "B", Points: 4}, "B")
	if err != nil || e.Awarded != 4 || e.Possible != 4 || !e.Correct {
		t.Fatalf("%+v %v", e, err)
	}
}
