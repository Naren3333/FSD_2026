package main

import (
	"context"
	"errors"
	"learning/platform"
)

// relation decides how the caller may access a classroom, optionally on behalf of a specific student: as
// its teacher, as an enrolled student, or not at all ("").
func (s server) relation(ctx context.Context, c platform.Claims, classroom, student string) (string, error) {
	if !platform.ID(classroom) {
		return "", nil
	}
	target := student
	if target == "" {
		target = c.Subject
	}
	org, teacher, enrolled, err := s.store.classroomAccess(ctx, classroom, target)
	if errors.Is(err, platform.ErrNotFound) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return decide(c, org, teacher, student, enrolled), nil
}

// decide is relation's policy, given the classroom's organization and teacher and whether the student asked
// about (the caller when none) is enrolled.
func decide(c platform.Claims, org, teacher, student string, enrolled bool) string {
	if org != c.Org {
		return ""
	}
	if c.Has(platform.RoleTeacher) && teacher == c.Subject {
		if student == "" || enrolled {
			return platform.RelationTeacher
		}
		return ""
	}
	if !c.Has(platform.RoleStudent) || (student != "" && student != c.Subject) || !enrolled {
		return ""
	}
	return platform.RelationStudent
}
