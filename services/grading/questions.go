package main

import (
	"errors"
	"math/big"
	"regexp"
	"slices"
	"strings"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

// Question types, as Assessment publishes them.
const (
	typeMultipleChoice = "multiple_choice"
	typeNumerical      = "numerical"
	typeShortAnswer    = "short_answer"
	typeFreeResponse   = "free_response"
)

// questionKind marks answers to one question type. A new type is a new kind registered in questionKinds;
// evaluate doesn't change. An error means the event carried a question or answer the kind cannot read.
type questionKind interface {
	// mark reports whether the answer earns the question's points, or that only the teacher can decide.
	mark(q Question, answer string) (correct, needsTeacher bool, err error)
}

var questionKinds = map[string]questionKind{
	typeMultipleChoice: multipleChoice{},
	typeNumerical:      numerical{},
	typeShortAnswer:    shortAnswer{},
	typeFreeResponse:   freeResponse{},
}

// multipleChoice: the chosen option must be the answer key.
type multipleChoice struct{}

func (multipleChoice) mark(q Question, answer string) (bool, bool, error) {
	return answer == q.Answer, false, nil
}

// numerical: the answer must be within the tolerance of the key, compared as exact decimals.
type numerical struct{}

func (numerical) mark(q Question, answer string) (bool, bool, error) {
	if !decimal.MatchString(answer) || !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) {
		return false, false, errors.New("invalid decimal")
	}
	a, ok := new(big.Rat).SetString(answer)
	if !ok {
		return false, false, errors.New("invalid answer")
	}
	key, ok := new(big.Rat).SetString(q.Answer)
	if !ok {
		return false, false, errors.New("invalid key")
	}
	tolerance, ok := new(big.Rat).SetString(q.Tolerance)
	if !ok || tolerance.Sign() < 0 {
		return false, false, errors.New("invalid tolerance")
	}
	return new(big.Rat).Abs(new(big.Rat).Sub(a, key)).Cmp(tolerance) <= 0, false, nil
}

// shortAnswer: an answer matching an accepted answer (after normalizeShort) earns the points. Any other answer
// may still be right in other words, so the teacher decides; it is never marked wrong automatically.
type shortAnswer struct{}

func (shortAnswer) mark(q Question, answer string) (bool, bool, error) {
	if len(q.AcceptedAnswers) == 0 {
		return false, false, errors.New("short answer without accepted answers")
	}
	given := normalizeShort(answer, q.CaseSensitive)
	matched := slices.ContainsFunc(q.AcceptedAnswers, func(a string) bool { return normalizeShort(a, q.CaseSensitive) == given })
	return matched, !matched, nil
}

// normalizeShort is Assessment's rule: trimmed, inner whitespace collapsed, and lowercased unless case matters.
func normalizeShort(answer string, caseSensitive bool) string {
	answer = strings.Join(strings.Fields(answer), " ")
	if !caseSensitive {
		answer = strings.ToLower(answer)
	}
	return answer
}

// freeResponse: always marked by the teacher against its rubric.
type freeResponse struct{}

func (freeResponse) mark(q Question, _ string) (bool, bool, error) {
	if q.Rubric == nil || len(q.Rubric.Criteria) == 0 {
		return false, false, errors.New("free response without a rubric")
	}
	return false, true, nil
}

// evaluate marks one answer: the question's points when its kind says it is correct, none when wrong, and none
// yet when it needs the teacher.
func evaluate(q Question, answer string) (Evidence, error) {
	e := Evidence{Question: q.ID, Skill: q.Skill, Possible: max(q.Points, 1)}
	kind, ok := questionKinds[q.Type]
	if !ok {
		return e, errors.New("unsupported question type")
	}
	correct, needsTeacher, err := kind.mark(q, answer)
	if err != nil {
		return e, err
	}
	if needsTeacher {
		e.NeedsMarking = true
		return e, nil
	}
	e.Correct, e.MarkedBy = correct, markedAuto
	if correct {
		e.Awarded = e.Possible
	}
	return e, nil
}
