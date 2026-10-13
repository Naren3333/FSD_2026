package main

import "testing"

func TestNumericalAnswersAreExactWithinTheTolerance(t *testing.T) {
	q := Question{Type: typeNumerical, Answer: "0.3", Tolerance: "0.05"}
	for answer, want := range map[string]bool{"0.30": true, "0.35": true, "0.351": false, "0.25": true, "0.2": false} {
		got, err := numerical{}.correct(q, answer)
		if err != nil || got != want {
			t.Errorf("%s: got %v, %v", answer, got, err)
		}
	}
	if _, err := (numerical{}).correct(q, "about 0.3"); err == nil {
		t.Fatal("scored a non-decimal answer")
	}
}

func TestAnUnknownTypeCannotBeScored(t *testing.T) {
	if _, err := evaluate(Question{ID: "q1", Type: "essay"}, "x"); err == nil {
		t.Fatal("scored an essay question")
	}
}
