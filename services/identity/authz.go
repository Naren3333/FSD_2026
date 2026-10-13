package main

import (
	"context"
	"errors"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// Relations the caller can have with a classroom.
const (
	relationTeacher = "teacher" // the classroom's own teacher
	relationStudent = "student" // an enrolled student
)

// relation decides how the caller may access a classroom, optionally on behalf of a specific student: as its
// teacher, as an enrolled student, or not at all (""). Teachers must own the classroom; students may only act for
// themselves, and the target student must be enrolled. The answer comes from ownership and enrollment, never from
// the token's role alone, so an account holding both roles is a student in a classroom it is enrolled in.
func (s server) relation(ctx context.Context, c platform.Claims, classroom, student string) (string, error) {
	if !platform.ID(classroom) {
		return "", nil
	}
	org, teacher, err := s.findClassroomOwner(ctx, classroom)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return decide(c, org, teacher, student, func(target string) (bool, error) { return s.isEnrolled(ctx, classroom, target) })
}

// decide is relation's policy, given the classroom's organization and teacher and an enrollment lookup.
func decide(c platform.Claims, org, teacher, student string, enrolled func(string) (bool, error)) (string, error) {
	if org != c.Org {
		return "", nil
	}
	if c.Has("teacher") && teacher == c.Subject {
		if student == "" {
			return relationTeacher, nil
		}
		ok, err := enrolled(student)
		if err != nil || !ok {
			return "", err
		}
		return relationTeacher, nil
	}
	if !c.Has("student") || (student != "" && student != c.Subject) {
		return "", nil
	}
	ok, err := enrolled(c.Subject)
	if err != nil || !ok {
		return "", err
	}
	return relationStudent, nil
}
