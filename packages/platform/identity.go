package platform

import (
	"encoding/json"
	"io"
	"net/http"
	"net/url"
)

// Relations Identity reports between the caller and a classroom.
const (
	RelationTeacher = "teacher"
	RelationStudent = "student"
)

// ClassroomAccess answers how the caller may access a classroom. Relation writes the error response itself when it
// denies access or cannot decide. The policy built on it (Access, TeacherOf) is in this package's functions, so the
// real implementation and any test double share it.
type ClassroomAccess interface {
	// Relation reports how the caller may access the classroom (optionally on behalf of a student): as its
	// teacher (RelationTeacher) or as an enrolled student (RelationStudent).
	Relation(w http.ResponseWriter, r *http.Request, classroom, student string) (string, bool)
}

// Access reports whether the caller may access the classroom at all, writing the refusal when not.
func Access(a ClassroomAccess, w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	_, ok := a.Relation(w, r, classroom, student)
	return ok
}

// TeacherOf allows only the classroom's own teacher. The token's teacher role is not enough: a teacher may only
// manage the classrooms Identity says they teach.
func TeacherOf(a ClassroomAccess, w http.ResponseWriter, r *http.Request, classroom string) bool {
	if !Teacher(w, r) {
		return false
	}
	relation, ok := a.Relation(w, r, classroom, "")
	if !ok {
		return false
	}
	if relation != RelationTeacher {
		Forbidden(w, "Only the classroom's teacher may do this.")
		return false
	}
	return true
}

// Identity asks the Identity service, forwarding the caller's own token, so classroom policy stays with its owner.
type Identity struct {
	base   string
	client *http.Client
}

// NewIdentity returns a ClassroomAccess backed by the Identity service at base (IDENTITY_URL).
func NewIdentity(base string, client *http.Client) *Identity {
	return &Identity{base: base, client: client}
}

// Relation asks Identity how the caller may access the classroom. Teacher-only data must be gated on
// RelationTeacher, not on the token role. On denial or failure it has already written the error response.
func (i *Identity) Relation(w http.ResponseWriter, r *http.Request, classroom, student string) (string, bool) {
	if !ID(classroom) {
		Invalid(w, "A valid classroom identifier is required.")
		return "", false
	}
	target := i.base + "/v1/classrooms/" + classroom + "/access"
	if student != "" {
		target += "?" + url.Values{"student_id": {student}}.Encode()
	}
	req, err := Forward(r, http.MethodGet, target)
	if err != nil {
		Unavailable(w, "Authorization unavailable.")
		return "", false
	}
	res, err := i.client.Do(req)
	if err != nil {
		Unavailable(w, "Authorization unavailable; retry later.")
		return "", false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		switch {
		case res.StatusCode >= 500:
			Unavailable(w, "Authorization unavailable.")
		case res.StatusCode == http.StatusUnauthorized:
			Unauthenticated(w, "The access token was not accepted.")
		default:
			Forbidden(w, "You do not have access to this classroom or student.")
		}
		return "", false
	}
	var answer struct {
		Relation string `json:"relation"`
	}
	if json.NewDecoder(io.LimitReader(res.Body, 4096)).Decode(&answer) != nil || (answer.Relation != RelationTeacher && answer.Relation != RelationStudent) {
		Unavailable(w, "Authorization answer was not understood.")
		return "", false
	}
	return answer.Relation, true
}
