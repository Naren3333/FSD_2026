package main

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is Identity's data: users, classrooms and enrollments.
type store interface {
	registerUser(ctx context.Context, org, subject, role, username, name string) error
	findClassroomOwner(ctx context.Context, classroom string) (org, teacher string, err error)
	isEnrolled(ctx context.Context, classroom, student string) (bool, error)
	insertClassroom(ctx context.Context, id, name, org, teacher string) error
	listClassrooms(ctx context.Context, org, subject string, isTeacher bool) ([]Classroom, error)
	studentExists(ctx context.Context, student, org string) (bool, error)
	studentsNamed(ctx context.Context, org, username string) ([]string, error)
	insertEnrollment(ctx context.Context, classroom, student string) error
	removeEnrollment(ctx context.Context, classroom, student string) (bool, error)
	listStudents(ctx context.Context, classroom string) ([]Student, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

// registerUser upserts the organization and the user's role and names in one transaction.
func (p pgStore) registerUser(ctx context.Context, org, subject, role, username, name string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "INSERT INTO organizations(id) VALUES($1) ON CONFLICT DO NOTHING", org)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO users(id,org_id,role,username,display_name) VALUES($1,$2,$3,NULLIF($4,''),NULLIF($5,'')) ON CONFLICT(id) DO UPDATE SET role=EXCLUDED.role,username=EXCLUDED.username,display_name=EXCLUDED.display_name WHERE users.org_id=EXCLUDED.org_id", subject, org, role, username, name)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return err
}

// findClassroomOwner returns the classroom's organization and teacher.
// It returns pgx.ErrNoRows when the classroom does not exist.
func (p pgStore) findClassroomOwner(ctx context.Context, classroom string) (org, teacher string, err error) {
	err = p.db.QueryRow(ctx, "SELECT org_id,teacher_id FROM classrooms WHERE id=$1", classroom).Scan(&org, &teacher)
	return org, teacher, err
}

func (p pgStore) isEnrolled(ctx context.Context, classroom, student string) (bool, error) {
	var enrolled bool
	err := p.db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM enrollments WHERE classroom_id=$1 AND student_id=$2)", classroom, student).Scan(&enrolled)
	return enrolled, err
}

func (p pgStore) insertClassroom(ctx context.Context, id, name, org, teacher string) error {
	_, err := p.db.Exec(ctx, "INSERT INTO classrooms(id,name,org_id,teacher_id) VALUES($1,$2,$3,$4)", id, name, org, teacher)
	return err
}

func (p pgStore) listClassrooms(ctx context.Context, org, subject string, isTeacher bool) ([]Classroom, error) {
	rows, err := p.db.Query(ctx, `SELECT c.id,c.name,c.teacher_id FROM classrooms c WHERE c.org_id=$1 AND ((c.teacher_id=$2 AND $3) OR EXISTS(SELECT 1 FROM enrollments e WHERE e.classroom_id=c.id AND e.student_id=$2)) ORDER BY c.created_at DESC LIMIT 100`, org, subject, isTeacher)
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

func (p pgStore) studentExists(ctx context.Context, student, org string) (bool, error) {
	var exists bool
	err := p.db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND role='student')", student, org).Scan(&exists)
	return exists, err
}

func (p pgStore) insertEnrollment(ctx context.Context, classroom, student string) error {
	_, err := p.db.Exec(ctx, "INSERT INTO enrollments(classroom_id,student_id) VALUES($1,$2) ON CONFLICT DO NOTHING", classroom, student)
	return err
}

// studentsNamed returns the ids of the organization's students with this username, ignoring case. More than one
// means the username is ambiguous.
func (p pgStore) studentsNamed(ctx context.Context, org, username string) ([]string, error) {
	rows, err := p.db.Query(ctx, "SELECT id FROM users WHERE org_id=$1 AND lower(username)=lower($2) AND role='student' LIMIT 2", org, username)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

// removeEnrollment reports whether the student was enrolled.
func (p pgStore) removeEnrollment(ctx context.Context, classroom, student string) (bool, error) {
	tag, err := p.db.Exec(ctx, "DELETE FROM enrollments WHERE classroom_id=$1 AND student_id=$2", classroom, student)
	return tag.RowsAffected() == 1, err
}

// listStudents is the classroom's roster, by name (students without one last, by id).
func (p pgStore) listStudents(ctx context.Context, classroom string) ([]Student, error) {
	rows, err := p.db.Query(ctx, `SELECT u.id,COALESCE(u.username,''),COALESCE(u.display_name,'') FROM enrollments e JOIN users u ON u.id=e.student_id WHERE e.classroom_id=$1 ORDER BY lower(COALESCE(u.display_name,u.username)) NULLS LAST,u.id LIMIT 500`, classroom)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Student, error) {
		var v Student
		err := row.Scan(&v.ID, &v.Username, &v.Name)
		return v, err
	})
}
