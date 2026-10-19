package platformtest

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"
)

// Schema is a JSON schema read from the repository's contracts (scripts/export_contracts.py writes them).
type Schema struct {
	node map[string]any
	doc  map[string]any // the document the schema is in, for its $refs
}

// HTTPSchema is the named schema components/schemas/<name> of contracts/http/<service>.openapi.json.
func HTTPSchema(t testing.TB, service, name string) Schema {
	t.Helper()
	doc := readContract(t, filepath.Join("contracts", "http", service+".openapi.json"))
	node, _ := dig(doc, "components", "schemas", name).(map[string]any)
	if node == nil {
		t.Fatalf("%s's contract has no schema %s", service, name)
	}
	return Schema{node, doc}
}

// EventData is the schema of the data of contracts/events/<topic>.schema.json.
func EventData(t testing.TB, topic string) Schema {
	t.Helper()
	doc := readContract(t, filepath.Join("contracts", "events", topic+".schema.json"))
	node, _ := dig(doc, "properties", "data").(map[string]any)
	if node == nil {
		t.Fatalf("%s has no data schema", topic)
	}
	return Schema{node, doc}
}

// Property is the object schema of one of the schema's properties: the property itself, its array items or map
// values, or its non-null choice.
func (s Schema) Property(t testing.TB, name string) Schema {
	t.Helper()
	property, _ := dig(s.resolve(s.node), "properties", name).(map[string]any)
	node, ok := s.object(property)
	if !ok {
		t.Fatalf("the schema has no object property %s", name)
	}
	return Schema{node, s.doc}
}

// MatchesSchema checks that the JSON fields of value's struct type are the schema's properties, and that the ones
// the struct always writes (no omitempty) are the ones the schema requires. Struct fields holding structs, or slices,
// maps or pointers of them, are checked against their property's schema the same way, so one call covers a
// response and everything in it. It catches a field added, renamed or made optional on one side only.
func MatchesSchema(t testing.TB, schema Schema, value any) {
	t.Helper()
	for _, problem := range schema.compare(reflect.TypeOf(value), reflect.TypeOf(value).Name()) {
		t.Error(problem)
	}
}

func (s Schema) compare(typ reflect.Type, at string) []string {
	node := s.resolve(s.node)
	properties, _ := node["properties"].(map[string]any)
	if properties == nil {
		return []string{at + ": the schema is not an object with properties"}
	}
	var required []string
	for _, name := range asList(node["required"]) {
		required = append(required, name.(string))
	}
	var problems []string
	seen := map[string]bool{}
	for _, f := range jsonFields(typ) {
		seen[f.name] = true
		property, ok := properties[f.name].(map[string]any)
		if !ok {
			problems = append(problems, at+"."+f.name+": not in the contract")
			continue
		}
		if f.always != slices.Contains(required, f.name) {
			problems = append(problems, at+"."+f.name+": required by the contract "+yesNo(!f.always)+
				", always written by the struct "+yesNo(f.always))
		}
		if inner := structType(f.typ); inner != nil {
			if nested, ok := s.object(property); ok {
				problems = append(problems, Schema{nested, s.doc}.compare(inner, at+"."+f.name)...)
			}
		}
	}
	for name := range properties {
		if !seen[name] {
			problems = append(problems, at+"."+name+": in the contract but not in the struct")
		}
	}
	slices.Sort(problems)
	return problems
}

// object is the object schema a property holds: itself, its array items or map values, or its non-null choice.
func (s Schema) object(property map[string]any) (map[string]any, bool) {
	property = s.resolve(property)
	if _, ok := property["properties"]; ok {
		return property, true
	}
	for _, key := range []string{"items", "additionalProperties"} {
		if inner, ok := property[key].(map[string]any); ok {
			return s.object(inner)
		}
	}
	for _, key := range []string{"oneOf", "anyOf"} {
		for _, choice := range asList(property[key]) {
			if inner, ok := choice.(map[string]any); ok {
				if found, ok := s.object(inner); ok {
					return found, true
				}
			}
		}
	}
	return nil, false
}

func (s Schema) resolve(node map[string]any) map[string]any {
	for {
		ref, ok := node["$ref"].(string)
		if !ok {
			return node
		}
		path := strings.Split(strings.TrimPrefix(ref, "#/"), "/")
		node, _ = dig(s.doc, path...).(map[string]any)
		if node == nil {
			return map[string]any{}
		}
	}
}

type jsonField struct {
	name   string
	always bool // written whatever its value: no omitempty
	typ    reflect.Type
}

// jsonFields are the fields encoding/json writes for a struct type, embedded structs' included.
func jsonFields(typ reflect.Type) []jsonField {
	var fields []jsonField
	for i := range typ.NumField() {
		f := typ.Field(i)
		tag := f.Tag.Get("json")
		if tag == "-" || (!f.IsExported() && !f.Anonymous) {
			continue
		}
		name, options, _ := strings.Cut(tag, ",")
		if f.Anonymous && name == "" && f.Type.Kind() == reflect.Struct {
			fields = append(fields, jsonFields(f.Type)...)
			continue
		}
		if name == "" {
			name = f.Name
		}
		fields = append(fields, jsonField{name, !slices.Contains(strings.Split(options, ","), "omitempty"), f.Type})
	}
	return fields
}

// structType is the struct a field holds, directly or through pointers, slices and maps; nil for anything else.
func structType(typ reflect.Type) reflect.Type {
	for typ.Kind() == reflect.Pointer || typ.Kind() == reflect.Slice || typ.Kind() == reflect.Map {
		typ = typ.Elem()
	}
	if typ.Kind() != reflect.Struct || typ == reflect.TypeFor[time.Time]() {
		return nil
	}
	return typ
}

func readContract(t testing.TB, rel string) map[string]any {
	t.Helper()
	dir, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	for {
		data, err := os.ReadFile(filepath.Join(dir, rel))
		if err == nil {
			var doc map[string]any
			if err := json.Unmarshal(data, &doc); err != nil {
				t.Fatalf("%s: %v", rel, err)
			}
			return doc
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			t.Fatalf("%s not found above the test's directory", rel)
		}
		dir = parent
	}
}

func dig(node any, path ...string) any {
	for _, key := range path {
		m, ok := node.(map[string]any)
		if !ok {
			return nil
		}
		node = m[key]
	}
	return node
}

func asList(v any) []any {
	list, _ := v.([]any)
	return list
}

func yesNo(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}
