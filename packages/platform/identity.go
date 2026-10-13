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

// ClassroomAccess decides what the caller may do in a classroom. Each method writes the error response itself
// when it denies access or cannot decide.
type ClassroomAccess interface {
	// Relation reports how the caller may access the classroom (optionally on behalf of a student): as its
	// teacher (RelationTeacher) or as an enrolled student (RelationStudent).
	Relation(w http.ResponseWriter, r *http.Request, classroom, student string) (string, bool)
	// Access reports whether the caller may access the classroom at all.
	Access(w http.ResponseWriter, r *http.Request, classroom, student string) bool
	// TeacherOf reports whether the caller is the classroom's own teacher.
	TeacherOf(w http.ResponseWriter, r *http.Request, classroom string) bool
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

// Access delegates classroom policy to its owner and forwards the verified caller token.
func (i *Identity) Access(w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	_, ok := i.Relation(w, r, classroom, student)
	return ok
}

// TeacherOf allows only the classroom's own teacher. The token's teacher role is not enough: an account with both
// roles that is enrolled in someone else's classroom is a student there.
func (i *Identity) TeacherOf(w http.ResponseWriter, r *http.Request, classroom string) bool {
	if !Teacher(w, r) {
		return false
	}
	relation, ok := i.Relation(w, r, classroom, "")
	if !ok {
		return false
	}
	if relation != RelationTeacher {
		Forbidden(w, "Only the classroom's teacher may do this.")
		return false
	}
	return true
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
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, target, nil)
	if err != nil {
		Unavailable(w, "Authorization unavailable.")
		return "", false
	}
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	req.Header.Set("X-Correlation-ID", Correlation(r.Context()))
	res, err := i.client.Do(req)
	if err != nil {
		Unavailable(w, "Authorization unavailable; retry later.")
		return "", false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		if res.StatusCode >= 500 {
			Unavailable(w, "Authorization unavailable.")
		} else {
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
