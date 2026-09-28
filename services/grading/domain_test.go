package main

import "testing"

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
