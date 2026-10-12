package main

import (
	"learning/platform"
	"strings"
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

// expectedParent maps each node kind to the kind its parent must have.
var expectedParent = map[string]string{"course": "subject", "unit": "course", "topic": "unit", "skill": "topic", "objective": "skill"}

func valid(v Input) bool {
	switch v.Kind {
	case "subject", "course", "unit", "topic", "skill", "objective":
	default:
		return false
	}
	return strings.TrimSpace(v.Name) != "" && len(v.Name) <= 160 && (v.Parent == nil || platform.ID(*v.Parent))
}
