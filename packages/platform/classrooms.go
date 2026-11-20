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

// TopicClassroomReassigned is identity's event announcing that a school admin gave a classroom another
// teacher (adr 0019): work billed to the classroom's teacher is billed to the new one from then on.
const TopicClassroomReassigned = "identity.classroom.reassigned.v1"

// ClassroomReassigned is the TopicClassroomReassigned event's data.
type ClassroomReassigned struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
	Teacher   string `json:"teacher_id"`
}

// DecodeClassroomReassigned reads a reassigned classroom.
func DecodeClassroomReassigned(event Event) (ClassroomReassigned, error) {
	var v ClassroomReassigned
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || v.Teacher == "" || event.Resource != v.Classroom || event.Timestamp.IsZero() {
		return ClassroomReassigned{}, InvalidEvent{Reason: "invalid classroom reassignment"}
	}
	return v, nil
}

// TopicEnrollmentRemoved is identity's event announcing that a student was removed from a classroom. its
// timestamp, like TopicEnrollmentAdded's, is the database's clock when the enrollment changed, so the two
// order one student's removals and returns.
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

// TopicEnrollmentAdded is identity's event announcing that a school admin enrolled a student in a
// classroom: grading marks again the work a student removed earlier submitted, once they are back.
const TopicEnrollmentAdded = "identity.enrollment.added.v1"

// EnrollmentAdded is the TopicEnrollmentAdded event's data.
type EnrollmentAdded struct {
	Classroom string `json:"classroom_id"`
	Org       string `json:"org_id"`
	Student   string `json:"student_id"`
}

// DecodeEnrollmentAdded reads an added enrollment.
func DecodeEnrollmentAdded(event Event) (EnrollmentAdded, error) {
	var v EnrollmentAdded
	if json.Unmarshal(event.Data, &v) != nil || !ID(v.Classroom) || v.Org == "" || v.Student == "" || event.Resource != v.Classroom || event.Timestamp.IsZero() {
		return EnrollmentAdded{}, InvalidEvent{Reason: "invalid enrollment"}
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
	return DiscardPendingBy(ctx, tx, "%", PurgeByClassroom, v.Classroom, v.Org)
}
