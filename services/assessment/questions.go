package main

import (
	"errors"
	"regexp"
	"slices"
	"strings"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

// Question types.
const (
	typeMultipleChoice = "multiple_choice"
	typeNumerical      = "numerical"
)

// questionKind holds one question type's rules: when a question of that type is well formed, and which answers a
// student may submit to it. A new type is a new kind registered in questionKinds; the callers don't change.
type questionKind interface {
	validate(q Question) error
	accepts(q Question, answer string) bool
}

var questionKinds = map[string]questionKind{
	typeMultipleChoice: multipleChoice{},
	typeNumerical:      numerical{},
}

// multipleChoice: 2–8 distinct options, and the answer key is one of them.
type multipleChoice struct{}

func (multipleChoice) validate(q Question) error {
	if len(q.Options) < minOptions || len(q.Options) > maxOptions {
		return errors.New("multiple choice needs 2–8 options")
	}
	seen := map[string]bool{}
	for _, o := range q.Options {
		if strings.TrimSpace(o) == "" || len(o) > maxAnswer || seen[o] {
			return errors.New("options must be unique nonempty values")
		}
		seen[o] = true
	}
	if !seen[q.Answer] {
		return errors.New("answer must match one option")
	}
	if q.Tolerance != "" {
		return errors.New("multiple choice has no tolerance")
	}
	return nil
}

func (multipleChoice) accepts(q Question, answer string) bool {
	return slices.Contains(q.Options, answer)
}

// numerical: a decimal answer key and a non-negative decimal tolerance, without options.
type numerical struct{}

func (numerical) validate(q Question) error {
	if !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) || strings.HasPrefix(q.Tolerance, "-") || len(q.Options) > 0 {
		return errors.New("numerical answers require decimal answer and nonnegative decimal tolerance, without options")
	}
	return nil
}

func (numerical) accepts(_ Question, answer string) bool { return decimal.MatchString(answer) }
