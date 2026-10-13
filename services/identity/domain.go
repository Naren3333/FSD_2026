package main

import "strings"

type Classroom struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Teacher string `json:"teacher_id"`
}

// maxName is the longest classroom name, in bytes.
const maxName = 120

func validName(name string) bool { return strings.TrimSpace(name) != "" && len(name) <= maxName }
