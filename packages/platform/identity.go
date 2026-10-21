package platform

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"slices"
)

// relations identity reports between the caller and a classroom.
const (
	RelationTeacher = "teacher"
	RelationStudent = "student"
)

// classroomaccess answers how the caller may access a classroom.
type ClassroomAccess interface {
	// relation reports how the caller may access the classroom (optionally on behalf of a student): as its
	// teacher (relationteacher) or as an enrolled student (relationstudent).
	Relation(w http.ResponseWriter, r *http.Request, classroom, student string) (string, bool)
}

// access reports whether the caller may access the classroom at all, writing the refusal when not.
func Access(a ClassroomAccess, w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	_, ok := a.Relation(w, r, classroom, student)
	return ok
}

// teacherof allows only the classroom's own teacher.
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

// identity asks the identity service, forwarding the caller's own token, so classroom policy stays with its
// owner.
type Identity struct {
	base   string
	client *http.Client
}

// newidentity returns a classroomaccess backed by the identity service at base (identity_url).
func NewIdentity(base string, client *http.Client) *Identity {
	return &Identity{base: base, client: client}
}

// relation asks identity how the caller may access the classroom.
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

// maxroster is the most of identity's id-only roster read, in bytes: about 100,000 account ids.
const maxRoster = 4 << 20

// classroomroster lists the students enrolled in a classroom, for its teacher.
type ClassroomRoster interface {
	// roster is the ids of every student enrolled in the classroom.
	Roster(w http.ResponseWriter, r *http.Request, classroom string) ([]string, bool)
}

// roster asks identity for the ids of the classroom's enrolled students with the caller's own token, so
// only the classroom's teacher gets an answer.
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
		// a roster larger than maxroster is cut off and fails to decode; say so, since its answer is a 503 like
		// any other unreadable roster.
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

// countingreader counts the bytes read through it.
type countingReader struct {
	r io.Reader
	n int64
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += int64(n)
	return n, err
}
