package main

import (
	"learning/platform"
	"slices"
	"strings"
	"unicode/utf8"
)

type Node struct {
	ID     string  `json:"id"`
	Kind   string  `json:"kind"`
	Name   string  `json:"name"`
	Parent *string `json:"parent_id"`
}
type Input struct {
	Kind   string  `json:"kind"`
	Name   string  `json:"name"`
	Parent *string `json:"parent_id"`
}

// kinds are the curriculum's levels, top down: a node's parent, when it has one, is the level above it. Any node may
// stand alone without a parent (teachers create standalone skills).
var kinds = []string{"subject", "course", "unit", "topic", "skill", "objective"}

// maxName is the longest node name, in characters.
const maxName = 160

// expectedParent maps each node kind to the kind its parent must have.
var expectedParent = func() map[string]string {
	parents := map[string]string{}
	for i := 1; i < len(kinds); i++ {
		parents[kinds[i]] = kinds[i-1]
	}
	return parents
}()

// validName reports whether the name, trimmed as it is stored, has 1–maxName characters.
func validName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && utf8.RuneCountInString(name) <= maxName
}

func valid(v Input) bool {
	return slices.Contains(kinds, v.Kind) && validName(v.Name) && (v.Parent == nil || platform.ID(*v.Parent))
}
