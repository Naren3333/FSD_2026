package main

import (
	"learning/platform/platformtest"
	"testing"
)

// verify response and event types against their generated contracts.
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
