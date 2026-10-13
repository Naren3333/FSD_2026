package main

import (
	"learning/platform"
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

func claims(subject string, roles ...string) platform.Claims {
	c := platform.Claims{Subject: subject, Org: "org"}
	c.Realm.Roles = roles
	return c
}

func TestDecideRelation(t *testing.T) {
	enrolled := map[string]bool{"student-1": true, "dual": true}
	lookup := func(id string) (bool, error) { return enrolled[id], nil }
	cases := []struct {
		name    string
		caller  platform.Claims
		org     string
		student string
		want    string
	}{
		{"owner", claims("teacher-1", "teacher"), "org", "", relationTeacher},
		{"owner asking about an enrolled student", claims("teacher-1", "teacher"), "org", "student-1", relationTeacher},
		{"owner asking about a stranger", claims("teacher-1", "teacher"), "org", "stranger", ""},
		{"another teacher", claims("teacher-2", "teacher"), "org", "", ""},
		{"enrolled student", claims("student-1", "student"), "org", "", relationStudent},
		{"enrolled student for themselves", claims("student-1", "student"), "org", "student-1", relationStudent},
		{"student asking about someone else", claims("student-1", "student"), "org", "dual", ""},
		{"student not enrolled", claims("stranger", "student"), "org", "", ""},
		{"other organization", claims("teacher-1", "teacher"), "elsewhere", "", ""},
		// A teacher enrolled as a student elsewhere is only a student there: no answer keys, rubrics or finalizing.
		{"dual-role account enrolled in another teacher's class", claims("dual", "teacher", "student"), "org", "", relationStudent},
		{"dual-role account that owns the class", claims("teacher-1", "teacher", "student"), "org", "", relationTeacher},
	}
	for _, tc := range cases {
		got, err := decide(tc.caller, tc.org, "teacher-1", tc.student, lookup)
		if err != nil || got != tc.want {
			t.Errorf("%s: got %q, %v; want %q", tc.name, got, err, tc.want)
		}
	}
}
