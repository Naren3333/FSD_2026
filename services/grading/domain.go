package main

import (
	"errors"
	"math/big"
	"regexp"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

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

func evaluate(q Question, answer string) (Evidence, error) {
	e := Evidence{Question: q.ID, Skill: q.Skill, Possible: 1}
	switch q.Type {
	case "multiple_choice":
		e.Correct = answer == q.Answer
	case "numerical":
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
