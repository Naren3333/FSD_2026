package main

import (
	"strings"
	"testing"
)

func TestEachKindDecidesWhichAnswersAStudentMaySubmit(t *testing.T) {
	choice := Question{Type: typeMultipleChoice, Options: []string{"1/2", "0.5"}, Answer: "1/2"}
	number := Question{Type: typeNumerical, Answer: "0.3", Tolerance: "0"}
	short := Question{Type: typeShortAnswer, AcceptedAnswers: []string{"Paris"}}
	written := Question{Type: typeFreeResponse, MaxLength: 5}
	cases := []struct {
		q      Question
		answer string
		want   bool
	}{
		{choice, "0.5", true},
		{choice, "half", false},
		{number, "0.30", true},
		{number, "three tenths", false},
		{short, "Lyon", true},
		{short, "  ", false},
		{short, strings.Repeat("a", maxAnswer+1), false},
		{short, strings.Repeat("語", maxAnswer), true}, // characters, not bytes, as saving counts them
		{written, "héllo", true},
		{written, "hello!", false},
		{written, "", false},
	}
	for _, tc := range cases {
		if got := questionKinds[tc.q.Type].accepts(tc.q, tc.answer); got != tc.want {
			t.Errorf("%s %q: got %v", tc.q.Type, tc.answer, got)
		}
	}
}

func TestAnUnknownTypeIsRejected(t *testing.T) {
	q := Question{Prompt: "Explain.", Type: "essay", Points: 1, Skill: "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"}
	if q.validate() == nil {
		t.Fatal("accepted an essay question")
	}
	if validateAnswers(Assessment{Questions: []Question{{ID: "q1", Type: "essay"}}}, map[string]string{"q1": "x"}) {
		t.Fatal("accepted an answer to an unknown type")
	}
}

func TestShortAnswersMatchAfterNormalizing(t *testing.T) {
	if normalizeShort("  New   York ", false) != "new york" || normalizeShort("DNA", true) != "DNA" {
		t.Fatal("normalization changed")
	}
}
