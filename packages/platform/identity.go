package platform

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"slices"
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

// maxRoster is the most of Identity's id-only roster read, in bytes: about 100,000 account ids.
const maxRoster = 4 << 20

// ClassroomRoster lists the students enrolled in a classroom, for its teacher. Roster writes the error response
// itself when it is refused or cannot answer.
type ClassroomRoster interface {
	// Roster is the ids of every student enrolled in the classroom.
	Roster(w http.ResponseWriter, r *http.Request, classroom string) ([]string, bool)
}

// Roster asks Identity for the ids of the classroom's enrolled students with the caller's own token, so only the
// classroom's teacher gets an answer. Work kept for students since removed (their submissions and grades) is left out
// of a teacher's lists with it, so it asks for the id-only roster, which has every student: the named one stops at 500.
// On denial or failure it has already written the error response.
func (i *Identity) Roster(w http.ResponseWriter, r *http.Request, classroom string) ([]string, bool) {
	if !ID(classroom) {
		Invalid(w, "A valid classroom identifier is required.")
		return nil, false
	}
	req, err := Forward(r, http.MethodGet, i.base+"/v1/classrooms/"+classroom+"/student-ids")
	if err != nil {
		Unavailable(w, "Authorization unavailable.")
		return nil, false
	}
	res, err := i.client.Do(req)
	if err != nil {
		Unavailable(w, "Authorization unavailable; retry later.")
		return nil, false
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		switch {
		case res.StatusCode >= 500:
			Unavailable(w, "Authorization unavailable.")
		case res.StatusCode == http.StatusUnauthorized:
			Unauthenticated(w, "The access token was not accepted.")
		default:
			Forbidden(w, "Only the classroom's teacher may do this.")
		}
		return nil, false
	}
	var ids []string
	body := &countingReader{r: io.LimitReader(res.Body, maxRoster)}
	if err = json.NewDecoder(body).Decode(&ids); err != nil || ids == nil || slices.Contains(ids, "") {
		// A roster larger than maxRoster is cut off and fails to decode; say so, since its answer is a 503 like any
		// other unreadable roster.
		cause := "unreadable"
		if body.n >= maxRoster {
			cause = "over the size limit"
		}
		slog.Error("classroom_roster_rejected", "classroom", classroom, "cause", cause, "bytes_read", body.n, "limit_bytes", maxRoster, "content_length", res.ContentLength)
		Unavailable(w, "The classroom's roster was not understood.")
		return nil, false
	}
	return ids, true
}

// countingReader counts the bytes read through it.
type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}
