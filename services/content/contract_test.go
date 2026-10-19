package main

import (
	"learning/platform/platformtest"
	"testing"
)

// The structs Content writes match the named schemas of its contract, so the web's generated types (and
// integration.py's response checks) describe what it really sends.
func TestResponsesMatchTheContract(t *testing.T) {
	for name, value := range map[string]any{
		"Document":       Document{},
		"SearchResponse": searchResponse{},
	} {
		t.Run(name, func(t *testing.T) {
			platformtest.MatchesSchema(t, platformtest.HTTPSchema(t, "content", name), value)
		})
	}
}
