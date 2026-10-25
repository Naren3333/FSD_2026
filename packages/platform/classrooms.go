package platform

import (
	"context"
	"encoding/json"

	"github.com/jackc/pgx/v5"
)

// TopicClassroomDeleted is identity's event announcing a deleted classroom: every service that keeps data
// for the classroom purges it.
const TopicClassroomDeleted = "identity.classroom.deleted.v1"

// ClassroomDeleted is the TopicClassroomDeleted event's data.
type ClassroomDeleted struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
}

// DecodeClassroomDeleted reads a classroom deletion.
func DecodeClassroomDeleted(event Event) (ClassroomDeleted, error) {
	var v ClassroomDeleted
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || event.Resource != v.Classroom {
		return ClassroomDeleted{}, InvalidEvent{Reason: "invalid classroom deletion"}
	}
	return v, nil
}

// TopicEnrollmentRemoved is identity's event announcing that a teacher removed a student from a classroom.
const TopicEnrollmentRemoved = "identity.enrollment.removed.v1"

// EnrollmentRemoved is the TopicEnrollmentRemoved event's data.
type EnrollmentRemoved struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
	Student   string `json:"student_id"`
}

// DecodeEnrollmentRemoved reads a removed enrollment.
func DecodeEnrollmentRemoved(event Event) (EnrollmentRemoved, error) {
	var v EnrollmentRemoved
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || v.Student == "" || event.Resource != v.Classroom {
		return EnrollmentRemoved{}, InvalidEvent{Reason: "invalid enrollment removal"}
	}
	return v, nil
}

// TopicUserDeleted is identity's event announcing that a user deleted their own account: every service that
// keeps data for the user purges it and refuses new data for them from then on.
const TopicUserDeleted = "identity.user.deleted.v1"

// UserDeleted is the TopicUserDeleted event's data.
type UserDeleted struct {
	User string `json:"user_id"`
	Org  string `json:"org_id"`
	Role string `json:"role"`
}

// DecodeUserDeleted reads a deleted account.
func DecodeUserDeleted(event Event) (UserDeleted, error) {
	var v UserDeleted
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.User) || v.Org == "" || (v.Role != "teacher" && v.Role != "student") || event.Resource != v.User {
		return UserDeleted{}, InvalidEvent{Reason: "invalid account deletion"}
	}
	return v, nil
}

// DiscardPending removes, inside tx, the outbox rows of events about the deleted classroom, so those not
// yet published never are.
func DiscardPending(ctx context.Context, tx pgx.Tx, v ClassroomDeleted) error {
	return DiscardPendingBy(ctx, tx, "%", "classroom_id", v.Classroom, v.Org)
}
