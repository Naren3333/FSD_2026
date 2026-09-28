package main

import "testing"

func TestValidation(t *testing.T) {
	for _, v := range []Input{{Kind: "unknown", Name: "X"}, {Kind: "skill", Name: " "}} {
		if valid(v) {
			t.Fatal("accepted invalid node")
		}
	}
	if !valid(Input{Kind: "skill", Name: "Fraction addition"}) {
		t.Fatal("rejected skill")
	}
}
