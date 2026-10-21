package main

import (
	"learning/platform"
	"learning/platform/platformtest"
	"testing"
)

func TestClassroomName(t *testing.T) {
	for _, name := range []string{"", "   "} {
		if validName(name) {
			t.Fatal("accepted empty classroom")
		}
	}
	if !validName("Fractions — Year 5") {
		t.Fatal("rejected classroom")
	}
}

var claims = platformtest.Claims

func TestDecideRelation(t *testing.T) {
	enrolled := map[string]bool{"student-1": true, "student-2": true, "teacher-2": true}
	lookup := func(id string) (bool, error) { return enrolled[id], nil }
	cases := []struct {
		name    string
		caller  platform.Claims
		org     string
		student string
		want    string
	}{
		{"owner", claims("teacher-1", "teacher"), "org", "", platform.RelationTeacher},
		{"owner asking about an enrolled student", claims("teacher-1", "teacher"), "org", "student-1", platform.RelationTeacher},
		{"owner asking about a stranger", claims("teacher-1", "teacher"), "org", "stranger", ""},
		{"another teacher", claims("teacher-2", "teacher"), "org", "", ""},
		{"enrolled student", claims("student-1", "student"), "org", "", platform.RelationStudent},
		{"enrolled student for themselves", claims("student-1", "student"), "org", "student-1", platform.RelationStudent},
		{"student asking about someone else", claims("student-1", "student"), "org", "student-2", ""},
		{"student not enrolled", claims("stranger", "student"), "org", "", ""},
		{"other organization", claims("teacher-1", "teacher"), "elsewhere", "", ""},
		// teacher powers come from owning the classroom, student access from enrollment and the student role.
		{"teacher listed as enrolled", claims("teacher-2", "teacher"), "org", "", ""},
		{"student named as the owner", claims("teacher-1", "student"), "org", "", ""},
	}
	for _, tc := range cases {
		got, err := decide(tc.caller, tc.org, "teacher-1", tc.student, lookup)
		if err != nil || got != tc.want {
			t.Errorf("%s: got %q, %v; want %q", tc.name, got, err, tc.want)
		}
	}
}
