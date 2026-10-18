package platform

import (
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"
)

// TopicClassroomDeleted is Identity's event announcing a deleted classroom: every service that keeps data for the
// classroom purges it.
const TopicClassroomDeleted = "identity.classroom.deleted.v1"

// ClassroomDeleted is the TopicClassroomDeleted event's data.
type ClassroomDeleted struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
}

// DecodeClassroomDeleted reads a classroom deletion. A malformed one, or one whose resource is not its classroom,
// is an InvalidEvent, so it is dead-lettered instead of retried.
func DecodeClassroomDeleted(event Event) (ClassroomDeleted, error) {
	var v ClassroomDeleted
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || event.Resource != v.Classroom {
		return ClassroomDeleted{}, InvalidEvent{Reason: "invalid classroom deletion"}
	}
	return v, nil
}

// TopicEnrollmentRemoved is Identity's event announcing that a teacher removed a student from a classroom. The
// student's submissions and grades are kept (they come back if the student is enrolled again), but nothing more is
// spent on them: Grading stops the student's AI marking for the classroom.
const TopicEnrollmentRemoved = "identity.enrollment.removed.v1"

// EnrollmentRemoved is the TopicEnrollmentRemoved event's data.
type EnrollmentRemoved struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
	Student   string `json:"student_id"`
}

// DecodeEnrollmentRemoved reads a removed enrollment. A malformed one, or one whose resource is not its classroom,
// is an InvalidEvent, so it is dead-lettered instead of retried.
func DecodeEnrollmentRemoved(event Event) (EnrollmentRemoved, error) {
	var v EnrollmentRemoved
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || v.Student == "" || event.Resource != v.Classroom {
		return EnrollmentRemoved{}, InvalidEvent{Reason: "invalid enrollment removal"}
	}
	return v, nil
}

// TopicUserDeleted is Identity's event announcing that a user deleted their own account: every service that keeps
// data for the user purges it and refuses new data for them from then on. A teacher deletes their classrooms first,
// so what remains of a teacher's is only what they did as themselves, such as chat threads.
const TopicUserDeleted = "identity.user.deleted.v1"

// UserDeleted is the TopicUserDeleted event's data. Role is the account's, teacher or student.
type UserDeleted struct {
	User string `json:"user_id"`
	Org  string `json:"org_id"`
	Role string `json:"role"`
}

// DecodeUserDeleted reads a deleted account. A malformed one, or one whose resource is not its user, is an
// InvalidEvent, so it is dead-lettered instead of retried.
func DecodeUserDeleted(event Event) (UserDeleted, error) {
	var v UserDeleted
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.User) || v.Org == "" || (v.Role != "teacher" && v.Role != "student") || event.Resource != v.User {
		return UserDeleted{}, InvalidEvent{Reason: "invalid account deletion"}
	}
	return v, nil
}

// DiscardPending removes, inside tx, the outbox rows of events about the deleted classroom, so those not yet
// published never are. The service's own data for the classroom is the caller's to delete in the same transaction.
func DiscardPending(ctx context.Context, tx pgx.Tx, v ClassroomDeleted) error {
	return DiscardPendingBy(ctx, tx, "%", "classroom_id", v.Classroom, v.Org)
}
