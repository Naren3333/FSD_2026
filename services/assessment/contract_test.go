package main

import (
	"learning/platform/platformtest"
	"testing"
)

// the structs assessment writes match the named schemas of its contract, so the web's generated types (and
// integration.py's response checks) describe what it really sends.
func TestResponsesMatchTheContract(t *testing.T) {
	for name, value := range map[string]any{
		"Assessment":         Assessment{},
		"StudentAssessment":  PublicAssessment{},
		"AssessmentSummary":  Summary{},
		"AssessmentProgress": Progress{},
		"OwnSubmission":      OwnSubmission{},
		"SavedAnswers":       SavedAnswers{},
	} {
		t.Run(name, func(t *testing.T) {
			platformtest.MatchesSchema(t, platformtest.HTTPSchema(t, "assessment", name), value)
		})
	}
}
