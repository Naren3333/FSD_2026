package main

import (
	"errors"
	"math/big"
	"regexp"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

// Evaluation statuses: a draft until the classroom's teacher finalizes it.
const (
	statusDraft     = "draft"
	statusFinalized = "finalized"
)

// Question types, as Assessment publishes them.
const (
	typeMultipleChoice = "multiple_choice"
	typeNumerical      = "numerical"
)

var (
	errScoreOutOfBounds = errors.New("override score out of bounds")
	errUnknownQuestion  = errors.New("override references unknown question")
)

type Question struct {
	ID        string `json:"id"`
	Type      string `json:"type"`
	Answer    string `json:"answer"`
	Tolerance string `json:"tolerance"`
	Skill     string `json:"skill_id"`
}
type Evidence struct {
	Question string `json:"question_id"`
	Skill    string `json:"skill_id"`
	Correct  bool   `json:"correct"`
	Awarded  int    `json:"awarded"`
	Possible int    `json:"possible"`
}
type Evaluation struct {
	ID         string     `json:"id"`
	Assessment string     `json:"assessment_id"`
	Classroom  string     `json:"classroom_id"`
	Student    string     `json:"student_id"`
	Status     string     `json:"status"`
	Evidence   []Evidence `json:"evidence"`
}

// applyOverrides updates evidence in place with the teacher's scores. It
// returns errScoreOutOfBounds at the first out-of-range score, or
// errUnknownQuestion if any override matched no question.
func applyOverrides(evidence []Evidence, overrides map[string]int) error {
	applied := 0
	for i, e := range evidence {
		if score, ok := overrides[e.Question]; ok {
			if score < 0 || score > e.Possible {
				return errScoreOutOfBounds
			}
			evidence[i].Awarded = score
			evidence[i].Correct = score == e.Possible
			applied++
		}
	}
	if applied != len(overrides) {
		return errUnknownQuestion
	}
	return nil
}

func evaluate(q Question, answer string) (Evidence, error) {
	e := Evidence{Question: q.ID, Skill: q.Skill, Possible: 1}
	switch q.Type {
	case typeMultipleChoice:
		e.Correct = answer == q.Answer
	case typeNumerical:
		if !decimal.MatchString(answer) || !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) {
			return e, errors.New("invalid decimal")
		}
		a, ok := new(big.Rat).SetString(answer)
		if !ok {
			return e, errors.New("invalid answer")
		}
		b, ok := new(big.Rat).SetString(q.Answer)
		if !ok {
			return e, errors.New("invalid key")
		}
		t, ok := new(big.Rat).SetString(q.Tolerance)
		if !ok || t.Sign() < 0 {
			return e, errors.New("invalid tolerance")
		}
		delta := new(big.Rat).Abs(new(big.Rat).Sub(a, b))
		e.Correct = delta.Cmp(t) <= 0
	default:
		return e, errors.New("unsupported question type")
	}
	if e.Correct {
		e.Awarded = 1
	}
	return e, nil
}
