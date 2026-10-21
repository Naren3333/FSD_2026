package main

import (
	"encoding/json"
	"fmt"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"os"
	"slices"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
)

// submittedschema is the published contract of the submission event.
func submittedSchema(t *testing.T) map[string]any {
	t.Helper()
	return eventSchema(t, topicSubmitted)
}

func eventSchema(t *testing.T, topic string) map[string]any {
	t.Helper()
	data, err := os.ReadFile("../../contracts/events/" + topic + ".schema.json")
	if err != nil {
		t.Fatal(err)
	}
	var schema map[string]any
	if err = json.Unmarshal(data, &schema); err != nil {
		t.Fatal(err)
	}
	return schema
}

// conform reports where value breaks schema, for the json schema keywords the event contracts use.
func conform(at string, schema map[string]any, value any) []string {
	var problems []string
	fail := func(format string, args ...any) { problems = append(problems, at+": "+fmt.Sprintf(format, args...)) }
	if c, ok := schema["const"]; ok && fmt.Sprint(c) != fmt.Sprint(value) {
		fail("%v is not %v", value, c)
	}
	if enum, ok := schema["enum"].([]any); ok && !slices.Contains(enum, value) {
		fail("%v is not one of %v", value, enum)
	}
	if kind, ok := schema["type"]; ok {
		kinds, _ := kind.([]any)
		if s, ok := kind.(string); ok {
			kinds = []any{s}
		}
		if !slices.ContainsFunc(kinds, func(k any) bool { return jsonType(value, k.(string)) }) {
			fail("%v is not %v", value, kind)
		}
	}
	number := func(key string) (float64, bool) { n, ok := schema[key].(float64); return n, ok }
	switch v := value.(type) {
	case string:
		if n, ok := number("minLength"); ok && utf8.RuneCountInString(v) < int(n) {
			fail("shorter than %v", n)
		}
		if n, ok := number("maxLength"); ok && utf8.RuneCountInString(v) > int(n) {
			fail("longer than %v", n)
		}
		switch schema["format"] {
		case "uuid":
			if uuid.Validate(v) != nil {
				fail("%q is not a uuid", v)
			}
		case "date-time":
			if _, err := time.Parse(time.RFC3339, v); err != nil {
				fail("%q is not RFC 3339", v)
			}
		}
	case float64:
		if n, ok := number("minimum"); ok && v < n {
			fail("below %v", n)
		}
		if n, ok := number("maximum"); ok && v > n {
			fail("above %v", n)
		}
	case []any:
		if n, ok := number("minItems"); ok && len(v) < int(n) {
			fail("fewer than %v items", n)
		}
		if n, ok := number("maxItems"); ok && len(v) > int(n) {
			fail("more than %v items", n)
		}
		if items, ok := schema["items"].(map[string]any); ok {
			for i, item := range v {
				problems = append(problems, conform(fmt.Sprintf("%s[%d]", at, i), items, item)...)
			}
		}
	case map[string]any:
		properties, _ := schema["properties"].(map[string]any)
		required, _ := schema["required"].([]any)
		for _, key := range required {
			if _, ok := v[key.(string)]; !ok {
				fail("missing %q", key)
			}
		}
		for key, field := range v {
			if property, ok := properties[key].(map[string]any); ok {
				problems = append(problems, conform(at+"."+key, property, field)...)
			} else if extra, ok := schema["additionalProperties"].(map[string]any); ok {
				problems = append(problems, conform(at+"."+key, extra, field)...)
			} else if schema["additionalProperties"] == false {
				fail("unexpected %q", key)
			}
		}
	}
	return problems
}

func jsonType(value any, kind string) bool {
	switch v := value.(type) {
	case nil:
		return kind == "null"
	case string:
		return kind == "string"
	case bool:
		return kind == "boolean"
	case float64:
		return kind == "number" || (kind == "integer" && v == float64(int64(v)))
	case []any:
		return kind == "array"
	case map[string]any:
		return kind == "object"
	}
	return false
}

// asjson is value as decoded json, as a consumer reads it.
func asJSON(t *testing.T, value any) any {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded any
	if err = json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
}

func TestTheSubmittedEventMatchesItsContract(t *testing.T) {
	s, store := newServer(platform.RelationTeacher, topicValid)
	v := post(t, s, allTypes)
	s.access = platformtest.Access(platform.RelationStudent)
	r := platformtest.Request(platformtest.Claims("student-1", "student"), http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", mixedAnswers(v))
	if w := platformtest.ServeRequest(s.routes, r); w.Code != http.StatusCreated || len(store.events) != 1 {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	data := submittedSchema(t)["properties"].(map[string]any)["data"].(map[string]any)
	event := asJSON(t, store.events[0])
	for _, problem := range conform("data", data, event) {
		t.Error(problem)
	}
	types := map[string]bool{}
	for _, q := range event.(map[string]any)["questions"].([]any) {
		types[q.(map[string]any)["type"].(string)] = true
	}
	for _, q := range event.(map[string]any)["questions"].([]any) {
		if q := q.(map[string]any); (q["type"] == typeShortAnswer || q["type"] == typeFreeResponse) && q["marking_scheme"] == nil {
			t.Errorf("%v has no marking scheme", q["type"])
		}
	}
	// where a generated question came from stays with the teacher (adr 0006).
	if body, _ := json.Marshal(event); strings.Contains(string(body), "sources") || strings.Contains(string(body), "generated") {
		t.Errorf("the event carries provenance: %s", body)
	}
	if len(types) != 4 || event.(map[string]any)["ai_marking"] != true || event.(map[string]any)["teacher_id"] != "user" {
		t.Fatalf("%v", event)
	}
}

// the checker itself must notice what the contract forbids, or the test above proves nothing.
func TestTheContractCheckFindsProblems(t *testing.T) {
	schema := submittedSchema(t)["properties"].(map[string]any)["data"].(map[string]any)
	v := fixture()
	v.ID, v.Questions[0].ID = uuid.NewString(), uuid.NewString()
	good := submittedEvent(v, Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()})
	if problems := conform("data", schema, asJSON(t, good)); len(problems) > 0 {
		t.Fatal(problems)
	}
	for name, change := range map[string]func(e map[string]any){
		"extra key":       func(e map[string]any) { e["grade"] = 1 },
		"missing key":     func(e map[string]any) { delete(e, "teacher_id") },
		"bad time":        func(e map[string]any) { e["submitted_at"] = "yesterday" },
		"extra question":  func(e map[string]any) { e["questions"].([]any)[0].(map[string]any)["hint"] = "x" },
		"no points":       func(e map[string]any) { e["questions"].([]any)[0].(map[string]any)["points"] = 0 },
		"unknown release": func(e map[string]any) { e["release_policy"] = "never" },
		"blank answer":    func(e map[string]any) { e["answers"] = map[string]any{"q": ""} },
		"blank passage": func(e map[string]any) {
			e["questions"].([]any)[0].(map[string]any)["marking_scheme"] = []any{map[string]any{"text": ""}}
		},
	} {
		e := asJSON(t, good).(map[string]any)
		change(e)
		if len(conform("data", schema, e)) == 0 {
			t.Errorf("%s: not noticed", name)
		}
	}
}
