package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"learning/platform"
	"slices"

	"github.com/jackc/pgx/v5"
)

// errNotAssigned is the answer to changing the list of an assessment that is for the whole class.
var errNotAssigned = errors.New("the assessment is for the whole class")

// errInvalidBankQuestion is the answer to a resit naming a bank question the teacher does not have.
var errInvalidBankQuestion = errors.New("not one of your bank questions")

// errStudentDeleted names a requested student whose account deletion is being purged.
type errStudentDeleted struct{ student string }

func (e errStudentDeleted) Error() string {
	return fmt.Sprintf("the account of %s was deleted", e.student)
}

// lockStudents takes each student's account lock shared, in order, and refuses any whose account was deleted:
// a purge either committed before (and is refused here) or waits for tx and then removes what tx wrote.
func lockStudents(ctx context.Context, tx pgx.Tx, students []string) error {
	sorted := slices.Sorted(slices.Values(students))
	for _, student := range slices.Compact(sorted) {
		if err := platform.LockShared(ctx, tx, platform.LockUser, student); err != nil {
			return err
		}
	}
	var deleted string
	err := tx.QueryRow(ctx, "SELECT user_id FROM deleted_users WHERE user_id=ANY($1) ORDER BY user_id LIMIT 1", sorted).Scan(&deleted)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		return nil
	case err != nil:
		return err
	}
	return errStudentDeleted{deleted}
}

// createResit stores v, a resit draft with its assigned students and links, in one transaction. the caller
// has checked the roster and topics; here only local checks run, under the students' and the classroom's
// locks, so neither purge can leave an orphan behind.
func (p pgStore) createResit(ctx context.Context, org string, v Assessment) error {
	data, err := json.Marshal(v.Questions)
	if err != nil {
		return err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockStudents(ctx, tx, v.Assignees); err != nil {
		return err
	}
	if err = platform.LockShared(ctx, tx, platform.LockClassroom, v.Classroom); err != nil {
		return err
	}
	var purged bool
	if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM purged_classrooms WHERE classroom_id=$1)", v.Classroom).Scan(&purged); err != nil {
		return err
	}
	if purged {
		return errClassroomPurged
	}
	var banks []string
	for _, bank := range v.BankLinks {
		if bank != "" && !slices.Contains(banks, bank) {
			banks = append(banks, bank)
		}
	}
	var owned int
	if err = tx.QueryRow(ctx, "SELECT count(*) FROM bank_questions WHERE id=ANY($1::uuid[]) AND owner_id=$2 AND org_id=$3", banks, v.LinkOwner, org).Scan(&owned); err != nil {
		return err
	}
	if owned != len(banks) {
		return errInvalidBankQuestion
	}
	if _, err = tx.Exec(ctx, "INSERT INTO assessments(id,classroom_id,org_id,title,questions,status,opens_at,due_at,late_policy,release_policy,ai_marking,published_at,created_by,assigned,resit_of) VALUES($1,$2,$3,$4,$5,'draft',NULL,NULL,$6,$7,$8,NULL,$9,true,$10)",
		v.ID, v.Classroom, org, v.Title, data, v.LatePolicy, v.ReleasePolicy, v.AIMarking, v.CreatedBy, v.ResitOf); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO assessment_assignees(assessment_id,student_id) SELECT $1,unnest($2::text[]) ON CONFLICT DO NOTHING", v.ID, v.Assignees); err != nil {
		return err
	}
	if err = writeLinks(ctx, tx, org, v); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// setAssignees replaces a resit's list with students. checked are the students the caller found on the
// roster; one added here that it did not check means the list changed meanwhile (errAssessmentChanged).
// only rows that change are written, so a kept student's purge is never undone; a removed student's saved
// work goes first, then their row, in the same order as the student's purge.
func (p pgStore) setAssignees(ctx context.Context, org, id string, students, checked []string) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockStudents(ctx, tx, students); err != nil {
		return err
	}
	var assigned bool
	if err = tx.QueryRow(ctx, "SELECT assigned FROM assessments WHERE id=$1 AND org_id=$2 FOR UPDATE", id, org).Scan(&assigned); err != nil {
		return err // platform.ErrNotFound when it is gone
	}
	if !assigned {
		return errNotAssigned
	}
	rows, err := tx.Query(ctx, "SELECT student_id FROM assessment_assignees WHERE assessment_id=$1", id)
	if err != nil {
		return err
	}
	current, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return err
	}
	added, removed, err := assigneeChanges(current, students, checked)
	if err != nil {
		return err
	}
	if err = removeAssignees(ctx, tx, id, removed); err != nil {
		return err
	}
	if len(added) > 0 {
		if _, err = tx.Exec(ctx, "INSERT INTO assessment_assignees(assessment_id,student_id) SELECT $1,unnest($2::text[]) ON CONFLICT DO NOTHING", id, added); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}

// assigneeChanges is who students adds to and removes from current; adding one the caller did not check
// means the list changed since it was read.
func assigneeChanges(current, students, checked []string) (added, removed []string, err error) {
	for _, student := range students {
		if !slices.Contains(current, student) {
			if !slices.Contains(checked, student) {
				return nil, nil, errAssessmentChanged
			}
			added = append(added, student)
		}
	}
	for _, student := range current {
		if !slices.Contains(students, student) {
			removed = append(removed, student)
		}
	}
	return added, removed, nil
}

// removeAssignees takes students off the list with their saved work, unless one has submitted.
func removeAssignees(ctx context.Context, tx pgx.Tx, id string, students []string) error {
	if len(students) == 0 {
		return nil
	}
	var submitted bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM submissions WHERE assessment_id=$1 AND student_id=ANY($2))", id, students).Scan(&submitted); err != nil {
		return err
	}
	if submitted {
		return errHasSubmissions
	}
	statements := []string{
		"DELETE FROM saved_answers WHERE assessment_id=$1 AND student_id=ANY($2)",
		"DELETE FROM assessment_assignees WHERE assessment_id=$1 AND student_id=ANY($2)",
	}
	return platform.ExecAll(ctx, tx, statements, id, students)
}

// findLinks is the bank question each of the assessment's questions came from, by question id, leaving out
// links to bank questions owner no longer has.
func (p pgStore) findLinks(ctx context.Context, org, assessment, owner string) (map[string]string, error) {
	rows, err := p.db.Query(ctx, `SELECT l.question_id::text,l.bank_question_id::text FROM question_bank_links l
		JOIN bank_questions b ON b.id=l.bank_question_id AND b.owner_id=$2 AND b.org_id=$3 WHERE l.assessment_id=$1`, assessment, owner, org)
	if err != nil {
		return nil, err
	}
	links := map[string]string{}
	var question, bank string
	_, err = pgx.ForEachRow(rows, []any{&question, &bank}, func() error {
		links[question] = bank
		return nil
	})
	return links, err
}
