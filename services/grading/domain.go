package main

import (
	"errors"
)

// Evaluation statuses: a draft until the classroom's teacher finalizes it.
const (
	statusDraft     = "draft"
	statusFinalized = "finalized"
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

// evaluate scores one answer: a whole mark when its question's kind says it is correct.
func evaluate(q Question, answer string) (Evidence, error) {
	e := Evidence{Question: q.ID, Skill: q.Skill, Possible: 1}
	kind, ok := questionKinds[q.Type]
	if !ok {
		return e, errors.New("unsupported question type")
	}
	correct, err := kind.correct(q, answer)
	if err != nil {
		return e, err
	}
	e.Correct = correct
	if correct {
		e.Awarded = e.Possible
	}
	return e, nil
}
