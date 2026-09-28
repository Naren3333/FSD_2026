package main

import (
	"encoding/json"
	"strings"
	"testing"
)

func fixture() Assessment {
	return Assessment{Classroom: "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b", Title: "Fractions", Questions: []Question{{ID: "q1", Prompt: "1 + 1?", Type: "numerical", Answer: "2", Tolerance: "0.01", Skill: "6ff7334b-1a5d-4ea3-aea9-a219dc94a39b"}}}
}
func TestAnswerKeysNeverPublic(t *testing.T) {
	v := fixture()
	b, err := json.Marshal(v.Public())
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "answer") || strings.Contains(string(b), "tolerance") {
		t.Fatal("leaked answer key")
	}
}
func TestNumericValidation(t *testing.T) {
	v := fixture()
	if v.Validate() != nil {
		t.Fatal("valid assessment rejected")
	}
	v.Questions[0].Tolerance = "-0.1"
	if v.Validate() == nil {
		t.Fatal("negative tolerance accepted")
	}
	v.Questions[0].Tolerance = "NaN"
	if v.Validate() == nil {
		t.Fatal("NaN accepted")
	}
}
func TestAnswers(t *testing.T) {
	v := fixture()
	for _, a := range []map[string]string{{}, {"other": "2"}, {"q1": "NaN"}, {"q1": "2", "other": "2"}} {
		if validateAnswers(v, a) {
			t.Fatal("invalid submission accepted")
		}
	}
	if !validateAnswers(v, map[string]string{"q1": "2.00"}) {
		t.Fatal("valid answer rejected")
	}
}
