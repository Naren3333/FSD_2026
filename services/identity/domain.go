package main

import "strings"

type Classroom struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Teacher string `json:"teacher_id"`
}

func validName(name string) bool { return strings.TrimSpace(name) != "" && len(name) <= 120 }
