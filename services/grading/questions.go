package main

import (
	"errors"
	"math/big"
	"regexp"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

// Question types, as Assessment publishes them.
const (
	typeMultipleChoice = "multiple_choice"
	typeNumerical      = "numerical"
)

// questionKind scores answers to one question type. A new type is a new kind registered in questionKinds;
// evaluate doesn't change. An error means the event carried a question or answer the kind cannot read.
type questionKind interface {
	correct(q Question, answer string) (bool, error)
}

var questionKinds = map[string]questionKind{
	typeMultipleChoice: multipleChoice{},
	typeNumerical:      numerical{},
}

// multipleChoice: the chosen option must be the answer key.
type multipleChoice struct{}

func (multipleChoice) correct(q Question, answer string) (bool, error) {
	return answer == q.Answer, nil
}

// numerical: the answer must be within the tolerance of the key, compared as exact decimals.
type numerical struct{}

func (numerical) correct(q Question, answer string) (bool, error) {
	if !decimal.MatchString(answer) || !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) {
		return false, errors.New("invalid decimal")
	}
	a, ok := new(big.Rat).SetString(answer)
	if !ok {
		return false, errors.New("invalid answer")
	}
	key, ok := new(big.Rat).SetString(q.Answer)
	if !ok {
		return false, errors.New("invalid key")
	}
	tolerance, ok := new(big.Rat).SetString(q.Tolerance)
	if !ok || tolerance.Sign() < 0 {
		return false, errors.New("invalid tolerance")
	}
	return new(big.Rat).Abs(new(big.Rat).Sub(a, key)).Cmp(tolerance) <= 0, nil
}
