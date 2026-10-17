package main

import (
	"strings"
	"unicode/utf8"
)

type Classroom struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Teacher string `json:"teacher_id"`
}

// Student is one enrolled student as a roster shows them. Username and Name are empty for a student who has not
// signed in since names were recorded.
type Student struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	Name     string `json:"display_name"`
}

// displayName is the name a roster shows: the token's full name, else its username, else nothing.
func displayName(name, username string) string {
	if name = strings.TrimSpace(name); name != "" {
		return name
	}
	return strings.TrimSpace(username)
}

// maxName is the longest classroom name, in characters.
const maxName = 120

// validName reports whether the name, trimmed as it is stored, has 1–maxName characters.
func validName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && utf8.RuneCountInString(name) <= maxName
}
