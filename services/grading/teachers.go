package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// reassign records a reassigned classroom's new teacher (adr 0019) and bills the classroom's draft ai
// marking to them, so a teacher who no longer has the classroom stops paying for it. an older reassignment
// arriving late changes nothing, and a purged classroom is not recorded again.
func reassign(ctx context.Context, tx pgx.Tx, event platform.Event) error {
	v, err := platform.DecodeClassroomReassigned(event)
	if err != nil {
		return err
	}
	// exclusive against a new evaluation in the classroom, which reads its teacher, and against its purge.
	if err = platform.Lock(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `INSERT INTO classroom_teachers(classroom_id,org_id,teacher_id,assigned_at)
		SELECT $1,$2,$3,$4 WHERE NOT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$1)
		ON CONFLICT(classroom_id) DO UPDATE SET teacher_id=EXCLUDED.teacher_id,assigned_at=EXCLUDED.assigned_at
		WHERE classroom_teachers.org_id=EXCLUDED.org_id AND classroom_teachers.assigned_at<EXCLUDED.assigned_at`,
		v.Classroom, v.Org, v.Teacher, event.Timestamp)
	if err != nil || tag.RowsAffected() == 0 {
		return err
	}
	// only evaluations with ai marking have a payer; a finalized grade is marked no more.
	_, err = tx.Exec(ctx, "UPDATE evaluations SET teacher_id=$3 WHERE classroom_id=$1 AND org_id=$2 AND teacher_id IS NOT NULL AND status='draft'", v.Classroom, v.Org, v.Teacher)
	return err
}
