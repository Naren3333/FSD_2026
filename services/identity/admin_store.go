package main

import (
	"context"
	"errors"
	"learning/platform"
	"time"

	"github.com/jackc/pgx/v5"
)

// lockTeacher holds the teacher's row shared until the transaction ends, so the account cannot be deleted
// meanwhile, or answers errNotATeacher when it is not a live teacher account of the organization.
func lockTeacher(ctx context.Context, tx pgx.Tx, org, teacher string) error {
	var found int
	err := tx.QueryRow(ctx, "SELECT 1 FROM users WHERE id=$1 AND org_id=$2 AND role='teacher' AND deleted_at IS NULL FOR SHARE", teacher, org).Scan(&found)
	if errors.Is(err, pgx.ErrNoRows) {
		return errNotATeacher
	}
	return err
}

// insertAccount records an account an admin created, before its first sign-in, so it can be assigned at once.
func (p pgStore) insertAccount(ctx context.Context, org, id, role, username, name string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "INSERT INTO organizations(id) VALUES($1) ON CONFLICT DO NOTHING", org); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO users(id,org_id,role,username,display_name) VALUES($1,$2,$3,$4,$5)", id, org, role, username, name); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// findAccount is the organization's live teacher or student account, or platform.ErrNotFound.
func (p pgStore) findAccount(ctx context.Context, org, id string) (Account, error) {
	var a Account
	err := p.db.QueryRow(ctx, "SELECT id,COALESCE(username,''),COALESCE(display_name,username,''),role FROM users WHERE id=$1 AND org_id=$2 AND role IN ('teacher','student') AND deleted_at IS NULL", id, org).
		Scan(&a.ID, &a.Username, &a.Name, &a.Role)
	return a, err
}

// accountOrder orders accounts by name, then id.
const accountOrder = "ORDER BY lower(COALESCE(display_name,username,'')),id"

// listAccounts is one page of the organization's live teacher or student accounts matching search, signed
// in or not, with how many match. search matches the name and the username after the school's id that starts
// every new one (signInName), so the school's id matches nobody in particular.
func (p pgStore) listAccounts(ctx context.Context, org, role, search string, offset, limit int) ([]Account, int, error) {
	const match = `FROM users WHERE org_id=$1 AND role=$2 AND deleted_at IS NULL AND ($3='' OR strpos(lower(COALESCE(display_name,'')||' '||
		COALESCE(CASE WHEN starts_with(username,$1||'.') THEN substr(username,length($1)+2) ELSE username END,'')),lower($3))>0)`
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*) "+match, org, role, search).Scan(&total); err != nil {
		return nil, 0, err
	}
	rows, err := p.db.Query(ctx, "SELECT id,COALESCE(username,''),COALESCE(display_name,username,''),role "+match+" "+accountOrder+" OFFSET $4 LIMIT $5", org, role, search, offset, limit)
	if err != nil {
		return nil, 0, err
	}
	accounts, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (Account, error) {
		var a Account
		return a, row.Scan(&a.ID, &a.Username, &a.Name, &a.Role)
	})
	return accounts, total, err
}

// listOrgClassrooms is one page of the organization's classrooms, newest first, with each one's teacher and
// how many students it has, and how many classrooms there are.
func (p pgStore) listOrgClassrooms(ctx context.Context, org string, offset, limit int) ([]SchoolClassroom, int, error) {
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*) FROM classrooms WHERE org_id=$1 AND deleted_at IS NULL", org).Scan(&total); err != nil {
		return nil, 0, err
	}
	// the page is chosen first, then its enrollments are counted in one pass.
	rows, err := p.db.Query(ctx, `WITH page AS (
			SELECT id,name,teacher_id,created_at FROM classrooms
			WHERE org_id=$1 AND deleted_at IS NULL ORDER BY created_at DESC,id DESC OFFSET $2 LIMIT $3
		), counts AS (
			SELECT classroom_id,count(*) AS students FROM enrollments WHERE classroom_id IN (SELECT id FROM page) GROUP BY classroom_id
		)
		SELECT c.id,c.name,c.teacher_id,COALESCE(u.display_name,u.username,''),COALESCE(n.students,0)
		FROM page c JOIN users u ON u.id=c.teacher_id LEFT JOIN counts n ON n.classroom_id=c.id
		ORDER BY c.created_at DESC,c.id DESC`, org, offset, limit)
	if err != nil {
		return nil, 0, err
	}
	classrooms, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (SchoolClassroom, error) {
		var c SchoolClassroom
		return c, row.Scan(&c.ID, &c.Name, &c.Teacher, &c.TeacherName, &c.Students)
	})
	return classrooms, total, err
}

// updateClassroom renames the organization's classroom or gives it another teacher, answering
// platform.ErrNotFound for no such live classroom and errNotATeacher for a teacher it cannot have. a new
// teacher is announced (identity.classroom.reassigned.v1), so grading bills the classroom's ai marking to them.
func (p pgStore) updateClassroom(ctx context.Context, org, id string, name, teacher *string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if teacher != nil {
		if err = lockTeacher(ctx, tx, org, *teacher); err != nil {
			return err
		}
	}
	var before string
	err = tx.QueryRow(ctx, "SELECT teacher_id FROM classrooms WHERE id=$1 AND org_id=$2 AND deleted_at IS NULL FOR UPDATE", id, org).Scan(&before)
	if errors.Is(err, pgx.ErrNoRows) {
		return platform.ErrNotFound
	}
	if err != nil {
		return err
	}
	// the database's clock, read under the classroom's lock, orders its reassignments as they commit, so
	// grading keeps the newest one whichever identity process made each (identity.classroom.reassigned.v1).
	var at time.Time
	if err = tx.QueryRow(ctx, "UPDATE classrooms SET name=COALESCE($2,name),teacher_id=COALESCE($3,teacher_id) WHERE id=$1 RETURNING clock_timestamp()", id, name, teacher).Scan(&at); err != nil {
		return err
	}
	if teacher != nil && *teacher != before {
		reassigned := platform.ClassroomReassigned{Classroom: id, Org: org, Teacher: *teacher}
		if err = platform.EmitAt(ctx, tx, platform.TopicClassroomReassigned, id, at, reassigned); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
