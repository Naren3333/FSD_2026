package main

import (
	"strings"
	"unicode/utf8"
)

// Topic is one entry of an organization's curriculum, the level questions, grades and analytics refer to.
type Topic struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// maxName is the longest topic name, in characters.
const maxName = 160

// validName reports whether the name, trimmed as it is stored, has 1–maxName characters.
func validName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && utf8.RuneCountInString(name) <= maxName
}
