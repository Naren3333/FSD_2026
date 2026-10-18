package main

import (
	"strings"
	"testing"
)

func TestValidation(t *testing.T) {
	for _, name := range []string{"", " ", strings.Repeat("x", maxName+1)} {
		if validName(name) {
			t.Fatalf("accepted %q", name)
		}
	}
	// The limit counts characters, not bytes, after trimming.
	if !validName(" Fraction addition ") || !validName(strings.Repeat("é", maxName)) {
		t.Fatal("rejected a valid name")
	}
}
