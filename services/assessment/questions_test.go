package main

import (
	"slices"
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

func TestEachKindRefusesMalformedQuestions(t *testing.T) {
	choice := func(change func(q *Question)) Question {
		q := Question{Type: typeMultipleChoice, Options: []string{"a", "b"}, Answer: "a"}
		change(&q)
		return q
	}
	short := func(change func(q *Question)) Question {
		q := Question{Type: typeShortAnswer, AcceptedAnswers: []string{"Paris"}}
		change(&q)
		return q
	}
	written := func(change func(q *Question)) Question {
		q := essay()
		q.MaxLength = defaultMaxLength
		change(&q)
		return q
	}
	many := make([]string, maxOptions+1)
	for i := range many {
		many[i] = string(rune('a' + i))
	}
	cases := map[string]Question{
		"answer not an option":      choice(func(q *Question) { q.Answer = "c" }),
		"duplicate options":         choice(func(q *Question) { q.Options = []string{"a", "a"} }),
		"blank option":              choice(func(q *Question) { q.Options = []string{"a", "  "} }),
		"long option":               choice(func(q *Question) { q.Options = []string{"a", strings.Repeat("b", maxAnswer+1)} }),
		"one option":                choice(func(q *Question) { q.Options = []string{"a"} }),
		"nine options":              choice(func(q *Question) { q.Options = many }),
		"choice with tolerance":     choice(func(q *Question) { q.Tolerance = "0" }),
		"choice with accepted":      choice(func(q *Question) { q.AcceptedAnswers = []string{"a"} }),
		"choice with max length":    choice(func(q *Question) { q.MaxLength = 10 }),
		"numerical with options":    {Type: typeNumerical, Options: []string{"1"}, Answer: "1", Tolerance: "0"},
		"short with options":        short(func(q *Question) { q.Options = []string{"Paris"} }),
		"short with answer":         short(func(q *Question) { q.Answer = "Paris" }),
		"short with tolerance":      short(func(q *Question) { q.Tolerance = "0" }),
		"short with rubric":         short(func(q *Question) { q.Rubric = essay().Rubric }),
		"short with max length":     short(func(q *Question) { q.MaxLength = 10 }),
		"too many accepted":         short(func(q *Question) { q.AcceptedAnswers = slices.Repeat([]string{"x"}, maxAccepted+1) }),
		"written with options":      written(func(q *Question) { q.Options = []string{"a"} }),
		"written with answer":       written(func(q *Question) { q.Answer = "a" }),
		"written with tolerance":    written(func(q *Question) { q.Tolerance = "0" }),
		"written with accepted":     written(func(q *Question) { q.AcceptedAnswers = []string{"a"} }),
		"written case sensitive":    written(func(q *Question) { q.CaseSensitive = true }),
		"written too long":          written(func(q *Question) { q.MaxLength = maxFreeResponse + 1 }),
		"model answer too long":     written(func(q *Question) { q.Rubric.ModelAnswer = strings.Repeat("é", maxModelAnswer+1) }),
		"eleven criteria":           written(func(q *Question) { q.Rubric.Criteria = slices.Repeat(q.Rubric.Criteria[1:], maxCriteria+1) }),
		"criterion without a title": written(func(q *Question) { q.Rubric.Criteria[0].Title = " " }),
	}
	for name, q := range cases {
		if questionKinds[q.Type].validate(q) == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	valid := []Question{
		choice(func(*Question) {}),
		choice(func(q *Question) { q.Options = many[:maxOptions] }),
		short(func(*Question) {}),
		written(func(q *Question) {
			q.MaxLength, q.Rubric.ModelAnswer = maxFreeResponse, strings.Repeat("é", maxModelAnswer)
		}),
	}
	for _, q := range valid {
		if err := questionKinds[q.Type].validate(q); err != nil {
			t.Errorf("%s refused: %v", q.Type, err)
		}
	}
}

func TestStoredQuestionsWithoutPointsAreWorthOne(t *testing.T) {
	questions, err := decodeQuestions([]byte(`[{"id":"q1","type":"numerical"},{"id":"q2","type":"free_response","points":7}]`))
	if err != nil || questions[0].Points != 1 || questions[1].Points != 7 {
		t.Fatalf("%+v %v", questions, err)
	}
	if _, err = decodeQuestions([]byte(`{`)); err == nil {
		t.Fatal("decoded malformed questions")
	}
}
