package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// allowed decides whether the caller may access a classroom, optionally on
// behalf of a specific student. Teachers must own the classroom; students may
// only act for themselves, and the target student must be enrolled.
func (s server) allowed(ctx context.Context, c platform.Claims, classroom, student string) (bool, error) {
	if !platform.ID(classroom) {
		return false, nil
	}
	org, teacher, err := s.findClassroomOwner(ctx, classroom)
	if err == pgx.ErrNoRows {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if org != c.Org {
		return false, nil
	}
	owner := c.Has("teacher") && teacher == c.Subject
	if !owner && (!c.Has("student") || (student != "" && student != c.Subject)) {
		return false, nil
	}
	target := student
	if !owner {
		target = c.Subject
	}
	if target == "" {
		return owner, nil
	}
	return s.isEnrolled(ctx, classroom, target)
}
