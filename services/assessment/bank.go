package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"learning/platform"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// bank limits and the lock that holds each teacher's bank creates against its count (adr 0017).
const (
	maxBank  = 2000
	lockBank = "bank"
)

// errBankFull is the answer to a save into a bank already holding maxBank questions.
var errBankFull = errors.New("the question bank is full")

// errAlreadyInBank names the bank question an assessment question already came from or was saved to.
type errAlreadyInBank struct{ id string }

func (e errAlreadyInBank) Error() string { return "already in the bank as " + e.id }

// a bank question is a teacher's question kept for reuse, its id the bank's: a question's fields without
// passages or marking points. answered is set when the list was asked about one student.
type BankQuestion struct {
	ID              string    `json:"id"`
	Prompt          string    `json:"prompt"`
	Type            string    `json:"type"`
	Options         []string  `json:"options"`
	Answer          string    `json:"answer"`
	Tolerance       string    `json:"tolerance"`
	AcceptedAnswers []string  `json:"accepted_answers,omitempty"`
	CaseSensitive   bool      `json:"case_sensitive,omitempty"`
	Rubric          *Rubric   `json:"rubric,omitempty"`
	MaxLength       int       `json:"max_length,omitempty"`
	Points          int       `json:"points"`
	Topic           string    `json:"topic_id"`
	Origin          string    `json:"origin,omitempty"`
	Answered        *bool     `json:"answered,omitempty"`
	CreatedAt       time.Time `json:"created_at"`
	UpdatedAt       time.Time `json:"updated_at"`
}

// question is b as an assessment's question, without an id yet.
func (b BankQuestion) question() Question {
	return Question{Prompt: b.Prompt, Type: b.Type, Options: b.Options, Answer: b.Answer, Tolerance: b.Tolerance,
		AcceptedAnswers: b.AcceptedAnswers, CaseSensitive: b.CaseSensitive, Rubric: b.Rubric, MaxLength: b.MaxLength,
		Points: b.Points, Topic: b.Topic, Origin: b.Origin}
}

// bankCopy is q as the bank keeps it. passages and marking points quote one classroom's documents, so they
// stay with that classroom's assessments.
func bankCopy(q Question) Question {
	q.ID, q.Sources, q.MarkingScheme = "", nil, nil
	return q
}

// bankFilter narrows a teacher's bank list. with student set, each question says whether that student has
// answered it, and exclude leaves those out.
type bankFilter struct {
	topics             []string
	kind, text, origin string
	student            string
	excludeAnswered    bool
}

// bankSource is the assessment question a bank question is saved from.
type bankSource struct{ assessment, question string }

// lockOwner holds the teacher's account lock shared until tx ends and refuses a deleted account, so no bank
// row lands after the account's purge.
func lockOwner(ctx context.Context, tx pgx.Tx, owner string) error {
	if err := platform.LockShared(ctx, tx, platform.LockUser, owner); err != nil {
		return err
	}
	var deleted bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM deleted_users WHERE user_id=$1)", owner).Scan(&deleted); err != nil {
		return err
	}
	if deleted {
		return errAccountDeleted
	}
	return nil
}

// insertBankQuestion keeps q, already checked by the caller, in owner's bank and returns its id. from, when
// set, is the assessment question it is saved from: it must still be there (errAssessmentChanged), must not
// already name one of owner's bank questions (errAlreadyInBank), and is linked to the new one.
func (p pgStore) insertBankQuestion(ctx context.Context, org, owner string, q Question, from *bankSource) (string, error) {
	q = bankCopy(q)
	data, err := json.Marshal(q)
	if err != nil {
		return "", err
	}
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	if err = lockOwner(ctx, tx, owner); err != nil {
		return "", err
	}
	if err = platform.Lock(ctx, tx, lockBank, owner); err != nil {
		return "", err
	}
	var held int
	if err = tx.QueryRow(ctx, "SELECT count(*) FROM bank_questions WHERE owner_id=$1 AND org_id=$2", owner, org).Scan(&held); err != nil {
		return "", err
	}
	if held >= maxBank {
		return "", errBankFull
	}
	if from != nil {
		// a question's id changes whenever its content does, so finding the id means the content the caller
		// checked is still the question's.
		var present bool
		if err = tx.QueryRow(ctx, "SELECT questions @> jsonb_build_array(jsonb_build_object('id',$2::text)) FROM assessments WHERE id=$1 AND org_id=$3 FOR SHARE", from.assessment, from.question, org).Scan(&present); err != nil {
			return "", err // platform.ErrNotFound when it is gone
		}
		if !present {
			return "", errAssessmentChanged
		}
		var existing string
		err = tx.QueryRow(ctx, `SELECT l.bank_question_id::text FROM question_bank_links l JOIN bank_questions b ON b.id=l.bank_question_id AND b.owner_id=$3
			WHERE l.assessment_id=$1 AND l.question_id=$2`, from.assessment, from.question, owner).Scan(&existing)
		switch {
		case err == nil:
			return "", errAlreadyInBank{existing}
		case !errors.Is(err, pgx.ErrNoRows):
			return "", err
		}
	}
	id := uuid.NewString()
	if _, err = tx.Exec(ctx, "INSERT INTO bank_questions(id,org_id,owner_id,question,topic_id,type,origin) VALUES($1,$2,$3,$4,$5,$6,nullif($7,''))",
		id, org, owner, data, q.Topic, q.Type, q.Origin); err != nil {
		return "", err
	}
	if from != nil {
		// replaces a link to a bank question since deleted.
		if _, err = tx.Exec(ctx, `INSERT INTO question_bank_links(assessment_id,question_id,bank_question_id) VALUES($1,$2,$3)
			ON CONFLICT(assessment_id,question_id) DO UPDATE SET bank_question_id=excluded.bank_question_id`, from.assessment, from.question, id); err != nil {
			return "", err
		}
	}
	return id, tx.Commit(ctx)
}

// updateBankQuestion replaces one of owner's bank questions with q, already checked by the caller.
func (p pgStore) updateBankQuestion(ctx context.Context, org, owner, id string, q Question) error {
	q = bankCopy(q)
	data, err := json.Marshal(q)
	if err != nil {
		return err
	}
	return p.ownerWrite(ctx, owner, func(tx pgx.Tx) (int64, error) {
		result, err := tx.Exec(ctx, "UPDATE bank_questions SET question=$4,topic_id=$5,type=$6,origin=nullif($7,''),updated_at=now() WHERE id=$1 AND owner_id=$2 AND org_id=$3",
			id, owner, org, data, q.Topic, q.Type, q.Origin)
		return result.RowsAffected(), err
	})
}

// deleteBankQuestion removes one of owner's bank questions; assessment copies, and links naming it, stay.
func (p pgStore) deleteBankQuestion(ctx context.Context, org, owner, id string) error {
	return p.ownerWrite(ctx, owner, func(tx pgx.Tx) (int64, error) {
		result, err := tx.Exec(ctx, "DELETE FROM bank_questions WHERE id=$1 AND owner_id=$2 AND org_id=$3", id, owner, org)
		return result.RowsAffected(), err
	})
}

// ownerWrite runs write under owner's account lock, platform.ErrNotFound when it touched no row.
func (p pgStore) ownerWrite(ctx context.Context, owner string, write func(tx pgx.Tx) (int64, error)) error {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = lockOwner(ctx, tx, owner); err != nil {
		return err
	}
	n, err := write(tx)
	if err == nil && n == 0 {
		err = platform.ErrNotFound
	}
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

const bankColumns = "b.id::text,b.question,b.created_at,b.updated_at"

func scanBankQuestion(row pgx.Row, extra ...any) (BankQuestion, error) {
	var v BankQuestion
	var id string
	var data []byte
	err := row.Scan(append([]any{&id, &data, &v.CreatedAt, &v.UpdatedAt}, extra...)...)
	if err == nil {
		err = json.Unmarshal(data, &v)
	}
	v.ID = id
	return v, err
}

func (p pgStore) findBankQuestion(ctx context.Context, org, owner, id string) (BankQuestion, error) {
	return scanBankQuestion(p.db.QueryRow(ctx, "SELECT "+bankColumns+" FROM bank_questions b WHERE b.id=$1 AND b.owner_id=$2 AND b.org_id=$3", id, owner, org))
}

// findBankQuestions is those of ids that are owner's bank questions, in no particular order.
func (p pgStore) findBankQuestions(ctx context.Context, org, owner string, ids []string) ([]BankQuestion, error) {
	rows, err := p.db.Query(ctx, "SELECT "+bankColumns+" FROM bank_questions b WHERE b.id=ANY($1::uuid[]) AND b.owner_id=$2 AND b.org_id=$3", ids, owner, org)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (BankQuestion, error) { return scanBankQuestion(row) })
}

// listBank is a page of owner's bank, newest change first, with the total f matches. "answered" counts a
// submission in any classroom, through the links owner's own assessments hold.
func (p pgStore) listBank(ctx context.Context, org, owner string, f bankFilter, window platform.ListWindow) ([]BankQuestion, int, error) {
	args := []any{owner, org}
	arg := func(v any) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}
	where := []string{"b.owner_id=$1", "b.org_id=$2"}
	if len(f.topics) > 0 {
		where = append(where, "b.topic_id=ANY("+arg(f.topics)+"::uuid[])")
	}
	if f.kind != "" {
		where = append(where, "b.type="+arg(f.kind))
	}
	if f.origin != "" {
		where = append(where, "coalesce(b.origin,'')="+arg(f.origin))
	}
	if f.text != "" {
		where = append(where, "position(lower("+arg(f.text)+") in lower(b.question->>'prompt'))>0")
	}
	answered := "NULL::boolean"
	if f.student != "" {
		answered = "EXISTS(SELECT 1 FROM question_bank_links l JOIN submissions s ON s.assessment_id=l.assessment_id AND s.student_id=" + arg(f.student) + " WHERE l.bank_question_id=b.id)"
		if f.excludeAnswered {
			where = append(where, "NOT "+answered)
		}
	}
	condition := strings.Join(where, " AND ")
	counted := args
	if f.student != "" && !f.excludeAnswered {
		counted = args[:len(args)-1] // the student is only in the answered column, not the condition
	}
	var total int
	if err := p.db.QueryRow(ctx, "SELECT count(*) FROM bank_questions b WHERE "+condition, counted...).Scan(&total); err != nil {
		return nil, 0, err
	}
	query := "SELECT " + bankColumns + "," + answered + " FROM bank_questions b WHERE " + condition +
		" ORDER BY b.updated_at DESC,b.id OFFSET " + arg(window.Offset) + " LIMIT " + arg(window.Limit)
	rows, err := p.db.Query(ctx, query, args...)
	if err != nil {
		return nil, 0, err
	}
	items, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (BankQuestion, error) {
		var answered *bool
		v, err := scanBankQuestion(row, &answered)
		v.Answered = answered
		return v, err
	})
	return items, total, err
}
