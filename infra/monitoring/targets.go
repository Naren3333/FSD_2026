package main

import (
	_ "embed"
	"encoding/json"
)

// the applications and dead-letter topics the collector watches; the contract export reads the same file.
//
//go:embed targets.json
var targetsJSON []byte

type monitoredTargets struct {
	Services    []string `json:"services"`
	DeadLetters []string `json:"dead_letter_topics"`
}

var targets = func() monitoredTargets {
	var t monitoredTargets
	if err := json.Unmarshal(targetsJSON, &t); err != nil || len(t.Services) == 0 || len(t.DeadLetters) == 0 {
		panic("invalid monitoring targets")
	}
	return t
}()

var (
	services         = targets.Services
	deadLetterTopics = targets.DeadLetters
)
