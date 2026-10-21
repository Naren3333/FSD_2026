package main

import (
	"context"
	"errors"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is identity's data: users, classrooms and enrollments.
type store interface {
	countClassrooms(ctx context.Context, org, subject string, teacher bool) (int, error)
	registerUser(ctx context.Context, org, subject, role, username, name string) error
	findClassroomOwner(ctx context.Context, classroom string) (org, teacher string, err error)
	isEnrolled(ctx context.Context, classroom, student string) (bool, error)
	insertClassroom(ctx context.Context, id, name, org, teacher string) error
	listClassrooms(ctx context.Context, org, subject string, isTeacher bool, paging ...platform.ListWindow) ([]Classroom, error)
	studentExists(ctx context.Context, student, org string) (bool, error)
	studentsNamed(ctx context.Context, org, username string) ([]string, error)
	insertEnrollment(ctx context.Context, classroom, student string) error
	removeEnrollment(ctx context.Context, classroom, org, student string) (bool, error)
	listStudents(ctx context.Context, classroom string, offset, limit int) ([]Student, int, error)
	listStudentIDs(ctx context.Context, classroom string) ([]string, error)
	deleteClassroom(ctx context.Context, classroom, org string) error
	listOrgStudents(ctx context.Context, org, search string, offset, limit int) ([]Student, int, error)
	deleteAccount(ctx context.Context, org, subject string) error
}

// pgstore is the store in the service's own postgres database.
type pgStore struct{ db *pgxpool.Pool }

// errrolechanged is registeruser's answer when the account was registered with the other role.
var errRoleChanged = errors.New("the account was registered with another role")

// erraccountdeleted is registeruser's answer for an account its user deleted: it cannot be used again.
var errAccountDeleted = errors.New("the account was deleted")

// registeruser upserts the organization and the user's names in one transaction, recording the role on
// first registration.
func (p pgStore) registerUser(ctx context.Context, org, subject, role, username, name string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "INSERT INTO organizations(id) VALUES($1) ON CONFLICT DO NOTHING", org); err != nil {
		return err
	}
	var stored string
	err = tx.QueryRow(ctx, "INSERT INTO users(id,org_id,role,username,display_name) VALUES($1,$2,$3,NULLIF($4,''),NULLIF($5,'')) ON CONFLICT(id) DO UPDATE SET username=EXCLUDED.username,display_name=EXCLUDED.display_name WHERE users.org_id=EXCLUDED.org_id AND users.deleted_at IS NULL RETURNING role", subject, org, role, username, name).Scan(&stored)
	if errors.Is(err, pgx.ErrNoRows) {
		var deleted bool
		if err = tx.QueryRow(ctx, "SELECT deleted_at IS NOT NULL FROM users WHERE id=$1", subject).Scan(&deleted); err != nil {
			return err
		}
		if deleted {
			return errAccountDeleted
		}
		return tx.Commit(ctx) // registered in another organization: the account is left as it is
	}
	if err != nil {
		return err
	}
	if stored != role {
		return errRoleChanged
	}
	return tx.Commit(ctx)
}

// findclassroomowner returns the classroom's organization and teacher.
func (p pgStore) findClassroomOwner(ctx context.Context, classroom string) (org, teacher string, err error) {
	err = p.db.QueryRow(ctx, "SELECT org_id,teacher_id FROM classrooms WHERE id=$1 AND deleted_at IS NULL", classroom).Scan(&org, &teacher)
	return org, teacher, err
}

func (p pgStore) isEnrolled(ctx context.Context, classroom, student string) (bool, error) {
	var enrolled bool
	err := p.db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM enrollments WHERE classroom_id=$1 AND student_id=$2)", classroom, student).Scan(&enrolled)
	return enrolled, err
}

// errprofilerequired is insertclassroom's answer when the teacher (or their organization) has not
// registered yet, or the account was deleted.
var errProfileRequired = errors.New("the teacher has not registered")

// insertclassroom creates the classroom for a registered teacher.
func (p pgStore) insertClassroom(ctx context.Context, id, name, org, teacher string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var found int
	err = tx.QueryRow(ctx, "SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND deleted_at IS NULL FOR SHARE", teacher, org).Scan(&found)
	if errors.Is(err, pgx.ErrNoRows) {
		return errProfileRequired
	}
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "INSERT INTO classrooms(id,name,org_id,teacher_id) VALUES($1,$2,$3,$4)", id, name, org, teacher)
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23503" { // foreign_key_violation
		return errProfileRequired
	}
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (p pgStore) listClassrooms(ctx context.Context, org, subject string, isTeacher bool, paging ...platform.ListWindow) ([]Classroom, error) {
	window := platform.Window(paging, 100)
	rows, err := p.db.Query(ctx, `SELECT c.id,c.name,c.teacher_id FROM classrooms c WHERE c.org_id=$1 AND c.deleted_at IS NULL AND ((c.teacher_id=$2 AND $3) OR EXISTS(SELECT 1 FROM enrollments e WHERE e.classroom_id=c.id AND e.student_id=$2)) ORDER BY c.created_at DESC,c.id DESC OFFSET $4 LIMIT $5`, org, subject, isTeacher, window.Offset, window.Limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Classroom, error) {
		var v Classroom
		err := row.Scan(&v.ID, &v.Name, &v.Teacher)
		return v, err
	})
}

func (p pgStore) studentExists(ctx context.Context, student, org string) (bool, error) {
	var exists bool
	err := p.db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND role='student' AND deleted_at IS NULL)", student, org).Scan(&exists)
	return exists, err
}

// errstudentdeleted is insertenrollment's answer when the student deleted their account meanwhile.
var errStudentDeleted = errors.New("the student's account was deleted")

// insertenrollment enrolls the student, or returns platform.errnotfound when the classroom was deleted
// meanwhile and errstudentdeleted when the student's account was.
func (p pgStore) insertEnrollment(ctx context.Context, classroom, student string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var found int
	if err = tx.QueryRow(ctx, "SELECT 1 FROM classrooms WHERE id=$1 AND deleted_at IS NULL FOR SHARE", classroom).Scan(&found); err != nil {
		return err
	}
	err = tx.QueryRow(ctx, "SELECT 1 FROM users WHERE id=$1 AND deleted_at IS NULL FOR SHARE", student).Scan(&found)
	if errors.Is(err, pgx.ErrNoRows) {
		return errStudentDeleted
	}
	if err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO enrollments(classroom_id,student_id) VALUES($1,$2) ON CONFLICT DO NOTHING", classroom, student); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// studentsnamed returns the ids of the organization's students with this username, ignoring case.
func (p pgStore) studentsNamed(ctx context.Context, org, username string) ([]string, error) {
	rows, err := p.db.Query(ctx, "SELECT id FROM users WHERE org_id=$1 AND lower(username)=lower($2) AND role='student' AND deleted_at IS NULL LIMIT 2", org, username)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

// removeenrollment unenrolls the student and announces it (platform.topicenrollmentremoved) in one
// transaction, and reports whether the student was enrolled.
func (p pgStore) removeEnrollment(ctx context.Context, classroom, org, student string) (bool, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, "DELETE FROM enrollments WHERE classroom_id=$1 AND student_id=$2", classroom, student)
	if err != nil || tag.RowsAffected() == 0 {
		return false, err
	}
	if err = platform.Emit(ctx, tx, platform.TopicEnrollmentRemoved, classroom, platform.EnrollmentRemoved{Classroom: classroom, Org: org, Student: student}); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}

// rosterorder orders a classroom's enrolled students by name, those without one last, by id.
const rosterOrder = "ORDER BY lower(COALESCE(u.display_name,u.username)) NULLS LAST,u.id"

// liststudents is one page of the classroom's roster with names, in rosterorder, with how many are enrolled
// in all.
func (p pgStore) listStudents(ctx context.Context, classroom string, offset, limit int) ([]Student, int, error) {
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*) FROM enrollments e JOIN users u ON u.id=e.student_id WHERE e.classroom_id=$1", classroom).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := p.db.Query(ctx, `SELECT u.id,COALESCE(u.username,''),COALESCE(u.display_name,'') FROM enrollments e JOIN users u ON u.id=e.student_id WHERE e.classroom_id=$1 `+rosterOrder+` OFFSET $2 LIMIT $3`, classroom, offset, limit)
	if err != nil {
		return nil, 0, err
	}
	students, err := pgx.CollectRows(rows, scanStudent)
	return students, total, err
}

// return the complete enrolled-student identifier set for authorization filters.
func (p pgStore) listStudentIDs(ctx context.Context, classroom string) ([]string, error) {
	rows, err := p.db.Query(ctx, "SELECT student_id FROM enrollments WHERE classroom_id=$1 ORDER BY student_id", classroom)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[string])
}

// page signed-in organization students with case-insensitive name and username filtering.
func (p pgStore) listOrgStudents(ctx context.Context, org, search string, offset, limit int) ([]Student, int, error) {
	const match = `FROM users WHERE org_id=$1 AND role='student' AND deleted_at IS NULL AND username IS NOT NULL AND ($2='' OR strpos(lower(COALESCE(display_name,'')||' '||username),lower($2))>0)`
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*) "+match, org, search).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := p.db.Query(ctx, "SELECT id,username,COALESCE(display_name,username) "+match+" ORDER BY lower(COALESCE(display_name,username)),id OFFSET $3 LIMIT $4", org, search, offset, limit)
	if err != nil {
		return nil, 0, err
	}
	students, err := pgx.CollectRows(rows, scanStudent)
	return students, total, err
}

func scanStudent(row pgx.CollectableRow) (Student, error) {
	var v Student
	err := row.Scan(&v.ID, &v.Username, &v.Name)
	return v, err
}

// deleteclassroom marks the classroom deleted, unenrolls its students and announces it, in one transaction,
// so no student stays tied to it and every other service purges its data for it.
func (p pgStore) deleteClassroom(ctx context.Context, classroom, org string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, "UPDATE classrooms SET deleted_at=now() WHERE id=$1 AND deleted_at IS NULL", classroom)
	if err != nil || tag.RowsAffected() == 0 {
		return err
	}
	_, err = tx.Exec(ctx, "DELETE FROM enrollments WHERE classroom_id=$1", classroom)
	if err == nil {
		err = platform.Emit(ctx, tx, platform.TopicClassroomDeleted, classroom, platform.ClassroomDeleted{Classroom: classroom, Org: org})
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	return err
}

// errownsclassrooms is deleteaccount's answer for a teacher who still has classrooms: deleting those is a
// separate, deliberate step (deleteclassroom), since it deletes every student's work in them.
var errOwnsClassrooms = errors.New("the teacher still has classrooms")

// clear the caller's profile and enrollments and announce deletion atomically.
func (p pgStore) deleteAccount(ctx context.Context, org, subject string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	// locked first, so a classroom or enrollment being created for the account (which holds the row shared)
	// commits before the classrooms are counted.
	var role string
	if err = tx.QueryRow(ctx, "SELECT role FROM users WHERE id=$1 AND org_id=$2 AND deleted_at IS NULL FOR UPDATE", subject, org).Scan(&role); err != nil {
		return err
	}
	var owned bool
	if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM classrooms WHERE teacher_id=$1 AND deleted_at IS NULL)", subject).Scan(&owned); err != nil {
		return err
	}
	if owned {
		return errOwnsClassrooms
	}
	if _, err = tx.Exec(ctx, "DELETE FROM enrollments WHERE student_id=$1", subject); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "UPDATE users SET deleted_at=now(),username=NULL,display_name=NULL WHERE id=$1", subject); err != nil {
		return err
	}
	if err = platform.Emit(ctx, tx, platform.TopicUserDeleted, subject, platform.UserDeleted{User: subject, Org: org, Role: role}); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (p pgStore) countClassrooms(ctx context.Context, org, subject string, teacher bool) (int, error) {
	var total int
	err := p.db.QueryRow(ctx, `SELECT count(*) FROM classrooms c WHERE c.org_id=$1 AND c.deleted_at IS NULL AND ((c.teacher_id=$2 AND $3) OR EXISTS(SELECT 1 FROM enrollments e WHERE e.classroom_id=c.id AND e.student_id=$2))`, org, subject, teacher).Scan(&total)
	return total, err
}
