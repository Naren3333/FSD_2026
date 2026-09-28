package main

import "testing"

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
