package main

import (
	"learning/platform/platformtest"
	"testing"
)

// The structs Identity writes match the named schemas of its contract, so the web's generated types (and
// integration.py's response checks) describe what it really sends.
func TestResponsesMatchTheContract(t *testing.T) {
	for name, value := range map[string]any{
		"Classroom": Classroom{},
		"Student":   Student{},
	} {
		t.Run(name, func(t *testing.T) {
			platformtest.MatchesSchema(t, platformtest.HTTPSchema(t, "identity", name), value)
		})
	}
}
