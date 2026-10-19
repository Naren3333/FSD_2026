package main

import (
	"learning/platform/platformtest"
	"testing"
)

// The structs Grading writes match the named schemas of its contract, so the web's generated types (and
// integration.py's response checks) describe what it really sends; and the events it reads and writes match theirs.
func TestResponsesMatchTheContract(t *testing.T) {
	for name, value := range map[string]any{
		"Evaluation":    Evaluation{},
		"OwnResult":     OwnResult{},
		"GradingCounts": Counts{},
		"Mark":          Mark{},
	} {
		t.Run(name, func(t *testing.T) {
			platformtest.MatchesSchema(t, platformtest.HTTPSchema(t, "grading", name), value)
		})
	}
}

func TestEventsMatchTheirSchemas(t *testing.T) {
	for topic, value := range map[string]any{
		topicSubmitted:         Submitted{},
		topicDueChanged:        DueChanged{},
		topicAssessmentDeleted: AssessmentDeleted{},
	} {
		t.Run(topic, func(t *testing.T) {
			platformtest.MatchesSchema(t, platformtest.EventData(t, topic), value)
		})
	}
	platformtest.MatchesSchema(t, platformtest.EventData(t, topicFinalized).Property(t, "evidence"), FinalEvidence{})
}
