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

// account is one teacher or student account of a school, as its admins see it.
type Account struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	Name     string `json:"display_name"`
	Role     string `json:"role"`
}

// schoolClassroom is one classroom as its school's admins see it.
type SchoolClassroom struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Teacher     string `json:"teacher_id"`
	TeacherName string `json:"teacher_name"`
	Students    int    `json:"students"`
}

// validUsername reports whether a new account's username has 3–64 lowercase letters, digits, dots,
// hyphens or underscores, starting with a letter or digit.
func validUsername(username string) bool {
	if len(username) < 3 || len(username) > 64 {
		return false
	}
	for i, r := range username {
		letterOrDigit := (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9')
		if !letterOrDigit && (i == 0 || !strings.ContainsRune("._-", r)) {
			return false
		}
	}
	return true
}

// signInName is the keycloak username of a new account of the organization: the organization's id, a dot and
// the username, so usernames are unique per school and a taken one says nothing about other schools. false
// when the id cannot start a username: 1–64 lowercase letters, digits or hyphens, so the first dot ends it.
func signInName(org, username string) (string, bool) {
	if org == "" || len(org) > 64 {
		return "", false
	}
	for _, r := range org {
		if (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '-' {
			return "", false
		}
	}
	return org + "." + username, true
}

// maxPersonName is the longest first or last name of a new account, in characters.
const maxPersonName = 60

// validPersonName reports whether the name, trimmed, has 1–maxPersonName characters.
func validPersonName(name string) bool {
	name = strings.TrimSpace(name)
	return name != "" && utf8.RuneCountInString(name) <= maxPersonName
}
