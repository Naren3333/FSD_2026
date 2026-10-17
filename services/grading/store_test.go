package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// These tests run the store and the marker against a real PostgreSQL (GRADING_TEST_DATABASE_URL); without it they
// are skipped. scripts/db_tests.py runs them against the Compose stack.

const (
	submissionA = "a1b2c3d4-0000-4000-8000-00000000000a"
	submissionB = "a1b2c3d4-0000-4000-8000-00000000000b"
	submissionC = "a1b2c3d4-0000-4000-8000-00000000000c"
)

func testDB(t *testing.T) *pgxpool.Pool {
	t.Helper()
	return platformtest.DB(t, "GRADING_TEST_DATABASE_URL", migrations)
}

// submit consumes a v2 submission, the free response of submittedEvent with AI marking unless change says otherwise.
func submit(t *testing.T, db *pgxpool.Pool, id, student string, change map[string]any) {
	t.Helper()
	data := map[string]any{"submission_id": id, "student_id": student}
	for k, v := range change {
		data[k] = v
	}
	event := submittedEvent(t, topicSubmittedV2, data)
	event.Resource = id
	tx, err := db.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(t.Context())
	if err = consume(t.Context(), tx, event); err != nil {
		t.Fatal(err)
	}
	if err = tx.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
}

// autoMarked is a submission change: one numerical question worth 2, answered correctly, released by hand.
var autoMarked = map[string]any{
	"questions":  []map[string]any{{"id": questionID, "prompt": "1+1?", "type": "numerical", "options": nil, "answer": "2", "tolerance": "0", "points": 2, "skill_id": skillID}},
	"answers":    map[string]string{questionID: "2"},
	"ai_marking": false,
}

type row struct {
	status   string
	evidence []Evidence
	released *time.Time
}

func load(t *testing.T, db *pgxpool.Pool, id string) row {
	t.Helper()
	var r row
	var data []byte
	if err := db.QueryRow(t.Context(), "SELECT status,evidence,released_at FROM evaluations WHERE id=$1", id).Scan(&r.status, &data, &r.released); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(data, &r.evidence); err != nil {
		t.Fatal(err)
	}
	return r
}

func count(t *testing.T, db *pgxpool.Pool, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := db.QueryRow(t.Context(), sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func events(t *testing.T, db *pgxpool.Pool, id string) int {
	return count(t, db, "SELECT count(*) FROM outbox WHERE topic=$1 AND partition_key=$2", topicFinalized, id)
}

func audits(t *testing.T, db *pgxpool.Pool, id string) int {
	return count(t, db, "SELECT count(*) FROM audit WHERE evaluation_id=$1", id)
}

func TestAManualReleaseShowsOnlyWhatWasFinalizedBeforeIt(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", autoMarked)
	submit(t, db, submissionB, "student-b", autoMarked)
	submit(t, db, submissionC, "student-c", autoMarked)
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", nil); err != nil {
		t.Fatal(err)
	}
	if events(t, db, submissionA) != 0 || load(t, db, submissionA).released != nil {
		t.Fatal("a manual-release grade went out at finalization")
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1"); err != nil || n != 1 {
		t.Fatalf("first release: %d %v", n, err)
	}
	if _, err := s.finalizeEvaluation(ctx, "org", submissionB, "teacher-1", "Checked", nil); err != nil {
		t.Fatal(err)
	}
	if load(t, db, submissionB).released != nil || events(t, db, submissionB) != 0 {
		t.Fatal("finalizing after the release released the grade")
	}
	if mine, err := s.listReleased(ctx, "org", submissionID, "student-b"); err != nil || len(mine) != 0 {
		t.Fatalf("student-b saw an unreleased grade: %+v %v", mine, err)
	}
	if mine, err := s.listReleased(ctx, "org", submissionID, "student-a"); err != nil || len(mine) != 1 {
		t.Fatalf("student-a lost their released grade: %+v %v", mine, err)
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1"); err != nil || n != 1 {
		t.Fatalf("second release: %d %v", n, err)
	}
	if events(t, db, submissionA) != 1 || events(t, db, submissionB) != 1 || load(t, db, submissionB).released == nil {
		t.Fatal("each grade must be released and published exactly once")
	}
	if c := load(t, db, submissionC); c.status != statusDraft || c.released != nil || events(t, db, submissionC) != 0 {
		t.Fatalf("a release touched a draft: %+v", c)
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1"); err != nil || n != 0 {
		t.Fatalf("a repeated release: %d %v", n, err)
	}
}

func TestAFinalizedGradeCannotBeEdited(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", map[string]any{"ai_marking": false})
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	before, trail := load(t, db, submissionA), audits(t, db, submissionA)
	_, err := s.saveMarks(ctx, "org", submissionA, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 0}, Feedback: text("Changed my mind.")}})
	if !errors.Is(err, errAlreadyFinalized) {
		t.Fatalf("edited a finalized grade: %v", err)
	}
	if after := load(t, db, submissionA); after.evidence[0].Awarded != 3 || after.evidence[0].Feedback != before.evidence[0].Feedback || audits(t, db, submissionA) != trail {
		t.Fatalf("a refused edit changed the grade: %+v", after)
	}
	if _, err = s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Again", nil); !errors.Is(err, errAlreadyFinalized) {
		t.Fatalf("finalized twice: %v", err)
	}
}

func TestConcurrentFinalizesPublishOnce(t *testing.T) {
	db := testDB(t)
	s := pgStore{db}
	submit(t, db, submissionA, "student-a", map[string]any{"ai_marking": false, "release_policy": "on_finalize"})
	marks := map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}
	// Hold the row so both finalizes have started and wait on it before either can go on.
	blocker, err := db.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer blocker.Rollback(context.Background())
	if _, err = blocker.Exec(t.Context(), "SELECT 1 FROM evaluations WHERE id=$1 FOR UPDATE", submissionA); err != nil {
		t.Fatal(err)
	}
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range errs {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, errs[i] = s.finalizeEvaluation(context.Background(), "org", submissionA, "teacher-1", "Checked", marks)
		}()
	}
	for deadline := time.Now().Add(5 * time.Second); count(t, db, "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'") < 2; {
		if time.Now().After(deadline) {
			t.Fatal("the finalizes never waited for the row")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err = blocker.Rollback(t.Context()); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	ok, conflict := 0, 0
	for _, err := range errs {
		switch {
		case err == nil:
			ok++
		case errors.Is(err, errAlreadyFinalized):
			conflict++
		default:
			t.Fatal(err)
		}
	}
	if ok != 1 || conflict != 1 || events(t, db, submissionA) != 1 || audits(t, db, submissionA) != 1 {
		t.Fatalf("%d finalized, %d refused, %d events", ok, conflict, events(t, db, submissionA))
	}
}

func TestFinalizeChecksItsRulesInTheStore(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", autoMarked)
	submit(t, db, submissionB, "student-b", map[string]any{"ai_marking": false})
	cases := []struct {
		name, id string
		marks    map[string]Mark
		want     error
	}{
		{"score above the question's points", submissionA, map[string]Mark{questionID: {Awarded: points(3)}}, errScoreOutOfBounds},
		{"unknown question", submissionA, map[string]Mark{skillID: {Awarded: points(1)}}, errUnknownQuestion},
		{"a free response left unmarked", submissionB, nil, errUnmarked},
		{"points on a rubric", submissionB, map[string]Mark{questionID: {Awarded: points(3)}}, errInvalidCriteria},
	}
	for _, tc := range cases {
		if _, err := s.finalizeEvaluation(ctx, "org", tc.id, "teacher-1", "Checked", tc.marks); !errors.Is(err, tc.want) {
			t.Errorf("%s: %v", tc.name, err)
		}
		if r := load(t, db, tc.id); r.status != statusDraft || audits(t, db, tc.id) != 0 || events(t, db, tc.id) != 0 {
			t.Errorf("%s: a refused finalize changed the evaluation: %+v", tc.name, r)
		}
	}
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Awarded: points(1)}}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", nil); !errors.Is(err, errAlreadyFinalized) {
		t.Fatalf("already final: %v", err)
	}
	if r := load(t, db, submissionA); r.evidence[0].Awarded != 1 || r.evidence[0].Correct {
		t.Fatalf("%+v", r.evidence[0])
	}
}

// fakeSuggester answers every marking call the same way; during runs before it answers.
type fakeSuggester struct {
	output string
	err    error
	calls  int
	who    platform.Billed
	during func()
}

func (f *fakeSuggester) generate(_ context.Context, _ []message, _ map[string]any, who platform.Billed) (string, string, error) {
	f.calls++
	f.who = who
	if f.during != nil {
		f.during()
	}
	return f.output, "model-1", f.err
}

// markOnce leases the only marking job and works it.
func markOnce(t *testing.T, m marker) job {
	t.Helper()
	j, found, err := m.lease(t.Context())
	if err != nil || !found {
		t.Fatalf("no job leased: %v", err)
	}
	if err = m.work(t.Context(), j); err != nil {
		t.Fatal(err)
	}
	return j
}

func jobs(t *testing.T, db *pgxpool.Pool) int {
	return count(t, db, "SELECT count(*) FROM marking_jobs")
}

func grounded3(t *testing.T) string {
	return reply(t, []map[string]any{criterion("c1", 3, "Because")}, "Say more.")
}

func TestAReadySuggestionNeverChangesTheGrade(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	markOnce(t, marker{db, ai})
	e := load(t, db, submissionA).evidence[0]
	if e.Suggestion == nil || e.Suggestion.Status != suggestionReady || e.Suggestion.Awarded != 3 || e.Suggestion.Feedback != "Say more." {
		t.Fatalf("suggestion not stored: %+v", e.Suggestion)
	}
	if e.Awarded != 0 || !e.NeedsMarking || e.MarkedBy != "" || e.Correct {
		t.Fatalf("a suggestion became a mark: %+v", e)
	}
	if ai.who != (platform.Billed{UserID: "teacher-1", OrgID: "org", Role: "teacher"}) {
		t.Fatalf("billed %+v", ai.who)
	}
	if jobs(t, db) != 0 || audits(t, db, submissionA) != 0 {
		t.Fatal("the job was not settled, or the suggestion was audited as a mark")
	}
}

func TestASuggestionAfterFinalizeIsDropped(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	ai.during = func() {
		if _, err := (pgStore{db}).finalizeEvaluation(context.Background(), "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 0}}}); err != nil {
			t.Error(err)
		}
	}
	m := marker{db, ai}
	j, found, err := m.lease(t.Context())
	if err != nil || !found {
		t.Fatal(err)
	}
	before := load(t, db, submissionA)
	if err = m.work(t.Context(), j); err != nil {
		t.Fatal(err)
	}
	after := load(t, db, submissionA)
	if ai.calls != 1 || after.status != statusFinalized || after.evidence[0].Awarded != 0 || after.evidence[0].Suggestion.Status != suggestionPending {
		t.Fatalf("a late suggestion reached a final grade: %+v (before %+v)", after, before)
	}
	if audits(t, db, submissionA) != 1 || jobs(t, db) != 0 {
		t.Fatal("audit or jobs changed after finalize")
	}
}

func TestMarkerOutcomesAreStoredAsFailedSuggestions(t *testing.T) {
	cases := []struct {
		name   string
		ai     *fakeSuggester
		before func(db *pgxpool.Pool)
		reason string
		calls  int
	}{
		{"teacher marked it first", &fakeSuggester{output: "{}"}, func(db *pgxpool.Pool) {
			if _, err := (pgStore{db}).saveMarks(context.Background(), "org", submissionA, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
				t.Fatal(err)
			}
		}, "MARKED_BY_TEACHER", 0},
		{"out of attempts", &fakeSuggester{output: "{}"}, func(db *pgxpool.Pool) {
			if _, err := db.Exec(context.Background(), "UPDATE marking_jobs SET attempts=$1,last_error='AI_PROVIDER_FAILED'", markingAttempts); err != nil {
				t.Fatal(err)
			}
		}, "AI_PROVIDER_FAILED", 0},
		{"ungrounded quote", &fakeSuggester{output: reply(t, []map[string]any{criterion("c1", 3, "a mutex")}, "")}, nil, "UNGROUNDED", 1},
		{"invalid output", &fakeSuggester{output: "Full marks!"}, nil, "INVALID_OUTPUT", 1},
		{"empty output", &fakeSuggester{err: &platform.AIError{Status: 502, Code: "EMPTY_OUTPUT"}}, nil, "EMPTY_OUTPUT", 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db := testDB(t)
			submit(t, db, submissionA, "student-a", nil)
			if tc.before != nil {
				tc.before(db)
			}
			awarded := load(t, db, submissionA).evidence[0].Awarded
			markOnce(t, marker{db, tc.ai})
			e := load(t, db, submissionA).evidence[0]
			s := e.Suggestion
			if s == nil || s.Status != suggestionFailed || s.Reason != tc.reason || s.Awarded != 0 || len(s.Criteria) != 0 || tc.ai.calls != tc.calls {
				t.Fatalf("%+v (calls %d)", s, tc.ai.calls)
			}
			if e.Awarded != awarded || jobs(t, db) != 0 {
				t.Fatalf("a failed suggestion changed the mark or kept its job: %+v", e)
			}
		})
	}
}

func TestALostLeaseWritesNothing(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	ai.during = func() { // another worker takes the job over meanwhile
		if _, err := db.Exec(context.Background(), "UPDATE marking_jobs SET leased_until=leased_until+interval '1 minute'"); err != nil {
			t.Error(err)
		}
	}
	markOnce(t, marker{db, ai})
	if s := load(t, db, submissionA).evidence[0].Suggestion; s.Status != suggestionPending || s.Reason != "" {
		t.Fatalf("a worker without the lease wrote %+v", s)
	}
	if jobs(t, db) != 1 {
		t.Fatal("a worker without the lease ended the job")
	}
}

func TestASpendLimitWaitsWithoutUsingAnAttempt(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	resets := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	markOnce(t, marker{db, &fakeSuggester{err: &platform.AIError{Status: 429, Code: "AI_SPEND_LIMIT", ResetsAt: resets}}})
	var attempts int
	var after time.Time
	if err := db.QueryRow(t.Context(), "SELECT attempts,run_after FROM marking_jobs").Scan(&attempts, &after); err != nil {
		t.Fatal(err)
	}
	if s := load(t, db, submissionA).evidence[0].Suggestion; attempts != 0 || !after.Equal(resets) || s.Status != suggestionPending || s.Reason != "AI_SPEND_LIMIT" {
		t.Fatalf("attempts %d, run after %v, %+v", attempts, after, s)
	}
}

// The real client against a fake ai-core: the teacher who set the assessment pays, and an empty reply fails.
func TestTheAssessmentsTeacherPaysForMarking(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", map[string]any{"teacher_id": "teacher-9"})
	f := newFakeAI(t, `{"output":null,"model_version":"m"}`)
	markOnce(t, marker{db, f.client()})
	if f.body["operation"] != "generate" || f.body["billed_to"].(map[string]any)["user_id"] != "teacher-9" || f.body["billed_to"].(map[string]any)["role"] != "teacher" {
		t.Fatalf("billed wrongly: %v", f.body)
	}
	if s := load(t, db, submissionA).evidence[0].Suggestion; s.Status != suggestionFailed || s.Reason != "EMPTY_OUTPUT" {
		t.Fatalf("%+v", s)
	}
}
