package platformtest

import (
	"learning/platform"
	"reflect"
	"testing"
)

// identity's events, which every service reads through platform, match their schemas.
func TestIdentityEventsMatchTheirSchemas(t *testing.T) {
	for topic, value := range map[string]any{
		platform.TopicClassroomDeleted:  platform.ClassroomDeleted{},
		platform.TopicEnrollmentRemoved: platform.EnrollmentRemoved{},
		platform.TopicUserDeleted:       platform.UserDeleted{},
	} {
		t.Run(topic, func(t *testing.T) { MatchesSchema(t, EventData(t, topic), value) })
	}
}

// a field on one side only, or optional on one side only, is reported; nested structs are compared too.
func TestMatchesSchemaReportsDrift(t *testing.T) {
	doc := map[string]any{"components": map[string]any{"schemas": map[string]any{
		"Inner": map[string]any{"properties": map[string]any{"b": map[string]any{}}, "required": []any{"b"}},
	}}}
	schema := Schema{map[string]any{
		"properties": map[string]any{
			"a":     map[string]any{},
			"inner": map[string]any{"type": "array", "items": map[string]any{"$ref": "#/components/schemas/Inner"}},
			"gone":  map[string]any{},
		},
		"required": []any{"a", "inner"},
	}, doc}
	type inner struct {
		B string `json:"b,omitempty"`
	}
	type value struct {
		A     string  `json:"a"`
		Inner []inner `json:"inner"`
		Extra int     `json:"extra"`
		Skip  int     `json:"-"`
	}
	got := schema.compare(reflect.TypeFor[value](), "value")
	want := []string{
		"value.extra: not in the contract",
		"value.gone: in the contract but not in the struct",
		"value.inner.b: required by the contract yes, always written by the struct no",
	}
	if len(got) != len(want) {
		t.Fatalf("%q", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("%q", got)
		}
	}
}
