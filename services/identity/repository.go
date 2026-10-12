package main

import "context"

// registerUser upserts the organization and the user's role in one transaction.
func (s server) registerUser(ctx context.Context, org, subject, role string) error {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "INSERT INTO organizations(id) VALUES($1) ON CONFLICT DO NOTHING", org)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO users(id,org_id,role) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET role=EXCLUDED.role WHERE users.org_id=EXCLUDED.org_id", subject, org, role)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return err
}

// findClassroomOwner returns the classroom's organization and teacher.
// It returns pgx.ErrNoRows when the classroom does not exist.
func (s server) findClassroomOwner(ctx context.Context, classroom string) (org, teacher string, err error) {
	err = s.DB.QueryRow(ctx, "SELECT org_id,teacher_id FROM classrooms WHERE id=$1", classroom).Scan(&org, &teacher)
	return org, teacher, err
}

func (s server) isEnrolled(ctx context.Context, classroom, student string) (bool, error) {
	var enrolled bool
	err := s.DB.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM enrollments WHERE classroom_id=$1 AND student_id=$2)", classroom, student).Scan(&enrolled)
	return enrolled, err
}

func (s server) insertClassroom(ctx context.Context, id, name, org, teacher string) error {
	_, err := s.DB.Exec(ctx, "INSERT INTO classrooms(id,name,org_id,teacher_id) VALUES($1,$2,$3,$4)", id, name, org, teacher)
	return err
}

func (s server) listClassrooms(ctx context.Context, org, subject string, isTeacher bool) ([]Classroom, error) {
	rows, err := s.DB.Query(ctx, `SELECT c.id,c.name,c.teacher_id FROM classrooms c WHERE c.org_id=$1 AND ((c.teacher_id=$2 AND $3) OR EXISTS(SELECT 1 FROM enrollments e WHERE e.classroom_id=c.id AND e.student_id=$2)) ORDER BY c.created_at DESC LIMIT 100`, org, subject, isTeacher)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Classroom{}
	for rows.Next() {
		var v Classroom
		if err = rows.Scan(&v.ID, &v.Name, &v.Teacher); err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

func (s server) studentExists(ctx context.Context, student, org string) (bool, error) {
	var exists bool
	err := s.DB.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND role='student')", student, org).Scan(&exists)
	return exists, err
}

func (s server) insertEnrollment(ctx context.Context, classroom, student string) error {
	_, err := s.DB.Exec(ctx, "INSERT INTO enrollments(classroom_id,student_id) VALUES($1,$2) ON CONFLICT DO NOTHING", classroom, student)
	return err
}
