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

// student is one enrolled student as a roster shows them.
type Student struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	Name     string `json:"display_name"`
}

// displayname is the name a roster shows: the token's full name, else its username, else nothing.
func displayName(name, username string) string {
	if name = strings.TrimSpace(name); name != "" {
		return name
	}
	return strings.TrimSpace(username)
}

// maxname is the longest classroom name, in characters.
const maxName = 120

// validname reports whether the name, trimmed as it is stored, has 1–maxname characters.
func validName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && utf8.RuneCountInString(name) <= maxName
}
