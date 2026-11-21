package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// these tests run the store and the marker against a real postgresql (GRADING_TEST_DATABASE_URL); without
// it they are skipped. scripts/db_tests.py runs them against the compose stack.

const (
	submissionA = "a1b2c3d4-0000-4000-8000-00000000000a"
	submissionB = "a1b2c3d4-0000-4000-8000-00000000000b"
	submissionC = "a1b2c3d4-0000-4000-8000-00000000000c"
)

func testDB(t *testing.T) *pgxpool.Pool {
	t.Helper()
	return platformtest.DB(t, "GRADING_TEST_DATABASE_URL", migrations)
}

// submit consumes a v4 submission, the free response of submittedEvent with ai marking unless change says
// otherwise.
func submit(t *testing.T, db *pgxpool.Pool, id, student string, change map[string]any) {
	t.Helper()
	data := map[string]any{"submission_id": id, "student_id": student}
	for k, v := range change {
		data[k] = v
	}
	event := submittedEvent(t, data)
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
	"questions":  []map[string]any{{"id": questionID, "prompt": "1+1?", "type": "numerical", "options": nil, "answer": "2", "tolerance": "0", "points": 2, "topic_id": topicID}},
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

// everyone is the roster of every student the tests submit as.
var everyone = []string{"student-a", "student-b", "student-c"}

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
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", everyone); err != nil || n != 1 {
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
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", everyone); err != nil || n != 1 {
		t.Fatalf("second release: %d %v", n, err)
	}
	if events(t, db, submissionA) != 1 || events(t, db, submissionB) != 1 || load(t, db, submissionB).released == nil {
		t.Fatal("each grade must be released and published exactly once")
	}
	if c := load(t, db, submissionC); c.status != statusDraft || c.released != nil || events(t, db, submissionC) != 0 {
		t.Fatalf("a release touched a draft: %+v", c)
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", everyone); err != nil || n != 0 {
		t.Fatalf("a repeated release: %d %v", n, err)
	}
}

func TestTheFinalizedEventAndStoredGradeNameTheTopic(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", map[string]any{"ai_marking": false, "release_policy": "on_finalize"})
	if _, err := (pgStore{db}).finalizeEvaluation(t.Context(), "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	var stored, payload []byte
	if err := db.QueryRow(t.Context(), "SELECT evidence->0 || questions->0 FROM evaluations WHERE id=$1", submissionA).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(t.Context(), "SELECT body FROM outbox WHERE topic=$1 AND partition_key=$2", "grading.finalized.v3", submissionA).Scan(&payload); err != nil {
		t.Fatal(err)
	}
	var item map[string]any
	if err := json.Unmarshal(stored, &item); err != nil || item["topic_id"] != topicID || item["skill_id"] != nil {
		t.Fatalf("stored: %s %v", stored, err)
	}
	var event struct {
		Data struct {
			Evidence []map[string]any `json:"evidence"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &event); err != nil || len(event.Data.Evidence) != 1 || event.Data.Evidence[0]["topic_id"] != topicID || event.Data.Evidence[0]["skill_id"] != nil {
		t.Fatalf("published: %s %v", payload, err)
	}
}

// TestTopicMigrationRenamesStoredSkillIDs stores grades as 003 left them, then checks 004 renames only the
// key.
func TestTopicMigrationRenamesStoredSkillIDs(t *testing.T) {
	db := platformtest.DB(t, "GRADING_TEST_DATABASE_URL", nil)
	ctx := t.Context()
	ordered, err := platform.Migrations(migrations)
	if err != nil || len(ordered) < 4 || ordered[3].Name != "004_topics.sql" {
		t.Fatalf("%v %v", ordered, err)
	}
	for _, m := range ordered[:3] {
		if _, err = db.Exec(ctx, m.SQL); err != nil {
			t.Fatalf("%s: %v", m.Name, err)
		}
	}
	evidence := `[{"question_id":"q1","skill_id":"s1","awarded":1,"suggestion":{"status":"ready"}},{"question_id":"q2","skill_id":"s2","awarded":0},"odd"]`
	questions := `[{"id":"q1","skill_id":"s1","rubric":{"criteria":[]}},{"id":"q2","topic_id":"t2","skill_id":"s2"}]`
	if _, err = db.Exec(ctx, "INSERT INTO evaluations(id,assessment_id,classroom_id,org_id,student_id,evidence,questions) VALUES($1,$1,$1,'org','s',$2,$3),($4,$4,$4,'org','s','[]',NULL)", submissionA, evidence, questions, submissionB); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(ctx, "INSERT INTO audit(evaluation_id,actor_id,action,reason,before_evidence,after_evidence) VALUES($1,'t','mark','r',$2,'[]')", submissionA, evidence); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(ctx, ordered[3].SQL); err != nil {
		t.Fatal(err)
	}
	want := map[string]string{
		"evidence":        `[{"awarded": 1, "topic_id": "s1", "suggestion": {"status": "ready"}, "question_id": "q1"}, {"awarded": 0, "topic_id": "s2", "question_id": "q2"}, "odd"]`,
		"questions":       `[{"id": "q1", "rubric": {"criteria": []}, "topic_id": "s1"}, {"id": "q2", "topic_id": "t2"}]`,
		"before_evidence": `[{"awarded": 1, "topic_id": "s1", "suggestion": {"status": "ready"}, "question_id": "q1"}, {"awarded": 0, "topic_id": "s2", "question_id": "q2"}, "odd"]`,
		"after_evidence":  `[]`,
	}
	var got [4]string
	if err = db.QueryRow(ctx, "SELECT v.evidence::text,v.questions::text,a.before_evidence::text,a.after_evidence::text FROM evaluations v JOIN audit a ON a.evaluation_id=v.id WHERE v.id=$1", submissionA).Scan(&got[0], &got[1], &got[2], &got[3]); err != nil {
		t.Fatal(err)
	}
	for i, column := range []string{"evidence", "questions", "before_evidence", "after_evidence"} {
		if got[i] != want[column] {
			t.Errorf("%s: %s", column, got[i])
		}
	}
	var empty string
	var none *string
	if err = db.QueryRow(ctx, "SELECT evidence::text,questions::text FROM evaluations WHERE id=$1", submissionB).Scan(&empty, &none); err != nil || empty != "[]" || none != nil {
		t.Fatalf("%q %v %v", empty, none, err)
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
	// hold the row so both finalizes have started and wait on it before either can go on.
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
		{"unknown question", submissionA, map[string]Mark{topicID: {Awarded: points(1)}}, errUnknownQuestion},
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

// fakeSuggester answers every marking call the same way, or a review call with review when it is set; during
// runs before it answers. it keeps each call's schema name and messages.
type fakeSuggester struct {
	output   string
	review   string
	err      error
	calls    int
	who      platform.Billed
	names    []string
	messages [][]message
	during   func()
}

func (f *fakeSuggester) generate(_ context.Context, name string, messages []message, _ map[string]any, who platform.Billed) (string, string, error) {
	f.calls++
	f.who = who
	f.names, f.messages = append(f.names, name), append(f.messages, messages)
	if name == schemaReview && f.review != "" {
		if f.during != nil {
			f.during()
		}
		return f.review, "model-2", f.err
	}
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
	markOnce(t, marker{db: db, ai: ai})
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
	m := marker{db: db, ai: ai}
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
		{"contradicted", &fakeSuggester{output: reply(t, []map[string]any{against(criterion("c1", 3, "Because"), "Because")}, "")}, nil, "CONTRADICTED", 1},
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
			markOnce(t, marker{db: db, ai: tc.ai})
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
	markOnce(t, marker{db: db, ai: ai})
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
	markOnce(t, marker{db: db, ai: &fakeSuggester{err: &platform.AIError{Status: 429, Code: "AI_SPEND_LIMIT", ResetsAt: resets}}})
	var attempts int
	var after time.Time
	if err := db.QueryRow(t.Context(), "SELECT attempts,run_after FROM marking_jobs").Scan(&attempts, &after); err != nil {
		t.Fatal(err)
	}
	if s := load(t, db, submissionA).evidence[0].Suggestion; attempts != 0 || !after.Equal(resets) || s.Status != suggestionPending || s.Reason != "AI_SPEND_LIMIT" {
		t.Fatalf("attempts %d, run after %v, %+v", attempts, after, s)
	}
}

// the real client against a fake ai-core: the teacher who set the assessment pays, and an empty reply
// fails.
func TestTheAssessmentsTeacherPaysForMarking(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", map[string]any{"teacher_id": "teacher-9"})
	f := newFakeAI(t, `{"output":null,"model_version":"m"}`)
	markOnce(t, marker{db: db, ai: f.client()})
	if f.body["operation"] != "generate" || f.body["billed_to"].(map[string]any)["user_id"] != "teacher-9" || f.body["billed_to"].(map[string]any)["role"] != "teacher" {
		t.Fatalf("billed wrongly: %v", f.body)
	}
	if s := load(t, db, submissionA).evidence[0].Suggestion; s.Status != suggestionFailed || s.Reason != "EMPTY_OUTPUT" {
		t.Fatalf("%+v", s)
	}
}

// deletion is the assessment.deleted.v1 event for the test submissions' assessment and classroom.
func deletion(t *testing.T) platform.Event {
	t.Helper()
	data, err := json.Marshal(AssessmentDeleted{Assessment: submissionID, Classroom: submissionID, Org: "org"})
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: topicAssessmentDeleted, Resource: submissionID, Data: data}
}

func purgeAssessmentNow(t *testing.T, db *pgxpool.Pool, event platform.Event) error {
	t.Helper()
	tx, err := db.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(t.Context())
	if err = purgeAssessment(t.Context(), tx, event); err != nil {
		return err
	}
	return tx.Commit(t.Context())
}

// assessment deletion removes grading data and prevents replay from recreating it.
func TestADeletedAssessmentLeavesNoGrades(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", map[string]any{"ai_marking": false, "release_policy": "on_finalize"})
	submit(t, db, submissionB, "student-b", nil) // ai marking queues a job
	other := "a1b2c3d4-0000-4000-8000-0000000000ff"
	submit(t, db, other, "student-a", map[string]any{"assessment_id": other, "ai_marking": false})
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	if events(t, db, submissionA) != 1 || audits(t, db, submissionA) == 0 || jobs(t, db) != 1 {
		t.Fatal("the fixture did not finalize, audit and queue as expected")
	}
	malformed := deletion(t)
	malformed.Resource = other
	var invalid platform.InvalidEvent
	if err := purgeAssessmentNow(t, db, malformed); !errors.As(err, &invalid) {
		t.Fatalf("a deletion naming another resource: %v", err)
	}
	for range 2 {
		if err := purgeAssessmentNow(t, db, deletion(t)); err != nil {
			t.Fatal(err)
		}
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE assessment_id=$1", submissionID); n != 0 {
		t.Fatalf("%d evaluations left", n)
	}
	if events(t, db, submissionA) != 0 || audits(t, db, submissionA) != 0 || jobs(t, db) != 0 {
		t.Fatal("the deleted assessment's events, audit or marking jobs were kept")
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE assessment_id=$1", other); n != 1 {
		t.Fatalf("another assessment lost its grade: %d", n)
	}
	submit(t, db, submissionC, "student-c", nil)
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE id=$1", submissionC); n != 0 {
		t.Fatal("a late submission event graded the deleted assessment")
	}
}

// a deletion is only for the assessment, classroom and organization it names: one missing a field is
// dead-lettered, and another organization's grade for an assessment with the same id is kept.
func TestADeletionNeedsItsFieldsAndKeepsToItsOrganization(t *testing.T) {
	db := testDB(t)
	elsewhere := "a1b2c3d4-0000-4000-8000-0000000000fe"
	submit(t, db, elsewhere, "student-a", map[string]any{"org_id": "org-b", "ai_marking": false})
	for name, change := range map[string]func(*AssessmentDeleted){
		"no organization":  func(d *AssessmentDeleted) { d.Org = "" },
		"a bad classroom":  func(d *AssessmentDeleted) { d.Classroom = "not-a-uuid" },
		"a bad assessment": func(d *AssessmentDeleted) { d.Assessment = "not-a-uuid" },
	} {
		d := AssessmentDeleted{Assessment: submissionID, Classroom: submissionID, Org: "org"}
		change(&d)
		data, err := json.Marshal(d)
		if err != nil {
			t.Fatal(err)
		}
		var invalid platform.InvalidEvent
		if err = purgeAssessmentNow(t, db, platform.Event{Type: topicAssessmentDeleted, Resource: submissionID, Data: data}); !errors.As(err, &invalid) {
			t.Errorf("%s: %v", name, err)
		}
	}
	if err := purgeAssessmentNow(t, db, deletion(t)); err != nil {
		t.Fatal(err)
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE id=$1 AND org_id='org-b'", elsewhere); n != 1 {
		t.Fatal("another organization lost its grade")
	}
}

// a marking job under way when its assessment is purged writes nothing: the job and its evaluation are
// gone.
func TestAMarkingJobPurgedMidwayWritesNothing(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionB, "student-b", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	ai.during = func() {
		if err := purgeAssessmentNow(t, db, deletion(t)); err != nil {
			t.Error(err)
		}
	}
	markOnce(t, marker{db: db, ai: ai})
	if ai.calls != 1 || jobs(t, db) != 0 || count(t, db, "SELECT count(*) FROM evaluations") != 0 {
		t.Fatalf("calls %d, jobs %d, evaluations %d", ai.calls, jobs(t, db), count(t, db, "SELECT count(*) FROM evaluations"))
	}
}

// apply runs one event's handler in its own transaction, as the consumer does.
func apply(t *testing.T, db *pgxpool.Pool, handle platform.Handler, event platform.Event) error {
	t.Helper()
	tx, err := db.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(t.Context())
	if err = handle(t.Context(), tx, event); err != nil {
		return err
	}
	return tx.Commit(t.Context())
}

// removal is identity.enrollment.removed.v1 for student-a in the test submissions' classroom, made at the
// given time.
func removal(t *testing.T, at time.Time) platform.Event {
	t.Helper()
	data, err := json.Marshal(platform.EnrollmentRemoved{Classroom: submissionID, Org: "org", Student: "student-a"})
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: platform.TopicEnrollmentRemoved, Resource: submissionID, Timestamp: at, Data: data}
}

// a removed student's work stays, hidden from the teacher's lists, and the suggestion waiting for it stops
// without an ai call.
func TestARemovedStudentsWorkIsKeptButNotMarked(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	submit(t, db, submissionB, "student-b", map[string]any{"submitted_at": submitted, "ai_marking": false})
	for range 2 { // a repeated event changes nothing
		if err := apply(t, db, removeStudent, removal(t, submitted.Add(time.Hour))); err != nil {
			t.Fatal(err)
		}
	}
	// an older, replayed removal does not move the latest one back.
	if err := apply(t, db, removeStudent, removal(t, submitted.Add(-time.Hour))); err != nil {
		t.Fatal(err)
	}
	ai := &fakeSuggester{output: grounded3(t)}
	markOnce(t, marker{db: db, ai: ai})
	if e := load(t, db, submissionA).evidence[0]; ai.calls != 0 || e.Suggestion == nil || e.Suggestion.Status != suggestionFailed || e.Suggestion.Reason != "STUDENT_REMOVED" {
		t.Fatalf("calls %d, suggestion %+v", ai.calls, e.Suggestion)
	}
	listed, total, err := s.listEvaluations(ctx, "org", submissionID, "", []string{"student-b"}, 0, 20)
	if err != nil || total != 1 || len(listed) != 1 || listed[0].Student != "student-b" {
		t.Fatalf("listed %+v of %d, %v", listed, total, err)
	}
	if c, err := s.countEvaluations(ctx, "org", submissionID, "", []string{"student-b"}); err != nil || c.Drafts != 1 {
		t.Fatalf("counts %+v, %v", c, err)
	}
	if c, err := s.countEvaluations(ctx, "org", submissionID, "", []string{"student-a", "student-b"}); err != nil || c.Drafts != 2 {
		t.Fatalf("counts once enrolled again %+v, %v", c, err)
	}
	submit(t, db, submissionC, "student-a", map[string]any{"submitted_at": submitted.Add(2 * time.Hour)})
	markOnce(t, marker{db: db, ai: ai})
	if e := load(t, db, submissionC).evidence[0]; ai.calls != 1 || e.Suggestion.Status != suggestionReady {
		t.Fatalf("work after enrolling again: calls %d, %+v", ai.calls, e.Suggestion)
	}
	malformed := removal(t, submitted)
	malformed.Resource = submissionA
	var invalid platform.InvalidEvent
	if err := apply(t, db, removeStudent, malformed); !errors.As(err, &invalid) {
		t.Fatalf("a removal naming another resource: %v", err)
	}
}

// enrollment is identity.enrollment.added.v1 for student-a in the test submissions' classroom, made at the
// given time.
func enrollment(t *testing.T, at time.Time) platform.Event {
	t.Helper()
	data, err := json.Marshal(platform.EnrollmentAdded{Classroom: submissionID, Org: "org", Student: "student-a"})
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: platform.TopicEnrollmentAdded, Resource: submissionID, Timestamp: at, Data: data}
}

// a student enrolled again after their removal gets ai suggestions for the work they submitted before it,
// whichever of the two events arrives first, until a newer removal.
func TestAStudentEnrolledAgainIsMarkedAgain(t *testing.T) {
	for _, returnFirst := range []bool{false, true} {
		db := testDB(t)
		submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
		submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
		events := []struct {
			handle platform.Handler
			event  platform.Event
		}{{removeStudent, removal(t, submitted.Add(time.Hour))}, {restoreStudent, enrollment(t, submitted.Add(2*time.Hour))}}
		if returnFirst {
			events[0], events[1] = events[1], events[0]
		}
		for _, e := range events {
			for range 2 { // a repeated event changes nothing
				if err := apply(t, db, e.handle, e.event); err != nil {
					t.Fatal(err)
				}
			}
		}
		// an older, replayed enrollment does not move the latest one back.
		if err := apply(t, db, restoreStudent, enrollment(t, submitted)); err != nil {
			t.Fatal(err)
		}
		ai := &fakeSuggester{output: grounded3(t)}
		markOnce(t, marker{db: db, ai: ai})
		if e := load(t, db, submissionA).evidence[0]; ai.calls != 1 || e.Suggestion == nil || e.Suggestion.Status != suggestionReady {
			t.Fatalf("return first %v: calls %d, suggestion %+v", returnFirst, ai.calls, e.Suggestion)
		}
	}
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	for _, e := range []struct {
		handle platform.Handler
		event  platform.Event
	}{{restoreStudent, enrollment(t, submitted.Add(time.Hour))}, {removeStudent, removal(t, submitted.Add(2*time.Hour))}} {
		if err := apply(t, db, e.handle, e.event); err != nil {
			t.Fatal(err)
		}
	}
	ai := &fakeSuggester{output: grounded3(t)}
	markOnce(t, marker{db: db, ai: ai})
	if e := load(t, db, submissionA).evidence[0]; ai.calls != 0 || e.Suggestion == nil || e.Suggestion.Reason != "STUDENT_REMOVED" {
		t.Fatalf("removed after returning: calls %d, suggestion %+v", ai.calls, e.Suggestion)
	}
	malformed := enrollment(t, time.Time{})
	var invalid platform.InvalidEvent
	if err := apply(t, db, restoreStudent, malformed); !errors.As(err, &invalid) {
		t.Fatalf("an enrollment without a time: %v", err)
	}
}

// work whose suggestion stopped while its student was out is queued again by a return newer than the removal,
// once however often it is delivered, and gets the suggestion; an older return leaves it stopped.
func TestAReturnQueuesAgainTheMarkingStoppedByARemoval(t *testing.T) {
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	if err := apply(t, db, removeStudent, removal(t, submitted.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	ai := &fakeSuggester{output: grounded3(t)}
	markOnce(t, marker{db: db, ai: ai})
	if e := load(t, db, submissionA).evidence[0]; ai.calls != 0 || jobs(t, db) != 0 || e.Suggestion.Reason != reasonStudentRemoved {
		t.Fatalf("removed: calls %d, jobs %d, %+v", ai.calls, jobs(t, db), e.Suggestion)
	}
	if err := apply(t, db, restoreStudent, enrollment(t, submitted.Add(30*time.Minute))); err != nil {
		t.Fatal(err)
	}
	if e := load(t, db, submissionA).evidence[0]; jobs(t, db) != 0 || e.Suggestion.Reason != reasonStudentRemoved {
		t.Fatalf("a return older than the removal queued it: jobs %d, %+v", jobs(t, db), e.Suggestion)
	}
	for range 2 { // a repeated return queues it once
		if err := apply(t, db, restoreStudent, enrollment(t, submitted.Add(2*time.Hour))); err != nil {
			t.Fatal(err)
		}
	}
	if jobs(t, db) != 1 {
		t.Fatalf("jobs %d", jobs(t, db))
	}
	markOnce(t, marker{db: db, ai: ai})
	if e := load(t, db, submissionA).evidence[0]; ai.calls != 1 || e.Suggestion.Status != suggestionReady {
		t.Fatalf("back in the classroom: calls %d, %+v", ai.calls, e.Suggestion)
	}
}

// a first enrollment, with no removal before it, queues nothing again: only a return does.
func TestAFirstEnrollmentQueuesNothingAgain(t *testing.T) {
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	// a stopped suggestion with no removal recorded, which only a return would queue again.
	if _, err := db.Exec(t.Context(), `DELETE FROM marking_jobs`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(t.Context(), `UPDATE evaluations SET evidence=jsonb_set(evidence,'{0,suggestion}',jsonb_build_object('status','failed','reason',$1::text)) WHERE id=$2`, reasonStudentRemoved, submissionA); err != nil {
		t.Fatal(err)
	}
	if err := apply(t, db, restoreStudent, enrollment(t, submitted.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	if e := load(t, db, submissionA).evidence[0]; jobs(t, db) != 0 || e.Suggestion == nil || e.Suggestion.Reason != reasonStudentRemoved {
		t.Fatalf("a first enrollment queued work again: jobs %d, %+v", jobs(t, db), e.Suggestion)
	}
}

// a worker that read the student as removed just before their return was recorded does not stop the
// suggestion: settling finds them back and runs the job again.
func TestASettlementRacingAReturnRunsTheJobAgain(t *testing.T) {
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	if err := apply(t, db, removeStudent, removal(t, submitted.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	ai := &fakeSuggester{output: grounded3(t)}
	m := marker{db: db, ai: ai}
	j, found, err := m.lease(t.Context())
	if err != nil || !found {
		t.Fatalf("no job leased: %v", err)
	}
	if err = apply(t, db, restoreStudent, enrollment(t, submitted.Add(2*time.Hour))); err != nil {
		t.Fatal(err)
	}
	if err = m.finish(t.Context(), j, replaced(&Suggestion{Status: suggestionFailed, Reason: reasonStudentRemoved})); err != nil {
		t.Fatal(err)
	}
	if e := load(t, db, submissionA).evidence[0]; jobs(t, db) != 1 || e.Suggestion.Status != suggestionPending {
		t.Fatalf("settled as removed after the return: jobs %d, %+v", jobs(t, db), e.Suggestion)
	}
	// the attempt that made no ai call is not counted.
	var attempts int
	if err = db.QueryRow(t.Context(), "SELECT attempts FROM marking_jobs").Scan(&attempts); err != nil || attempts != j.attempts-1 {
		t.Fatalf("attempts %d after leasing at %d, %v", attempts, j.attempts, err)
	}
	markOnce(t, m)
	if e := load(t, db, submissionA).evidence[0]; ai.calls != 1 || e.Suggestion.Status != suggestionReady {
		t.Fatalf("calls %d, %+v", ai.calls, e.Suggestion)
	}
}

// an enrollment or removal arriving after its classroom's purge or its student's deletion records nothing.
func TestAnEnrollmentAfterAPurgeIsNotRecorded(t *testing.T) {
	for _, purged := range []string{
		"INSERT INTO purged_classrooms(classroom_id,org_id) VALUES('" + submissionID + "','org')",
		"INSERT INTO deleted_users(user_id,org_id) VALUES('student-a','org')",
	} {
		db := testDB(t)
		if _, err := db.Exec(t.Context(), purged); err != nil {
			t.Fatal(err)
		}
		if err := apply(t, db, restoreStudent, enrollment(t, time.Now())); err != nil {
			t.Fatal(err)
		}
		if err := apply(t, db, removeStudent, removal(t, time.Now())); err != nil {
			t.Fatal(err)
		}
		if n := count(t, db, "SELECT count(*) FROM enrollment_returns") + count(t, db, "SELECT count(*) FROM removed_enrollments"); n != 0 {
			t.Fatalf("%s: %d rows recorded after it", purged, n)
		}
	}
}

// a classroom's purge and its student's account deletion both remove the returns recorded for them.
func TestPurgesRemoveEnrollmentReturns(t *testing.T) {
	student := "b1b2c3d4-0000-4000-8000-0000000000ee"
	for _, deleted := range []string{"classroom", "student"} {
		db := testDB(t)
		returned := enrollment(t, time.Now())
		data, err := json.Marshal(platform.EnrollmentAdded{Classroom: submissionID, Org: "org", Student: student})
		if err != nil {
			t.Fatal(err)
		}
		returned.Data = data
		if err = apply(t, db, restoreStudent, returned); err != nil {
			t.Fatal(err)
		}
		handle, event := purge, platform.Event{Type: platform.TopicClassroomDeleted, Resource: submissionID}
		event.Data, err = json.Marshal(platform.ClassroomDeleted{Classroom: submissionID, Org: "org"})
		if deleted == "student" {
			handle, event = purgeUser, platform.Event{Type: platform.TopicUserDeleted, Resource: student}
			event.Data, err = json.Marshal(platform.UserDeleted{User: student, Org: "org", Role: "student"})
		}
		if err == nil {
			err = apply(t, db, handle, event)
		}
		if err != nil {
			t.Fatal(err)
		}
		if n := count(t, db, "SELECT count(*) FROM enrollment_returns"); n != 0 {
			t.Fatalf("the %s purge left %d returns", deleted, n)
		}
	}
}

// dueChange is assessment.due_changed.v1 for the test submissions' assessment, made at the given time.
func dueChange(t *testing.T, due *time.Time, at time.Time) platform.Event {
	t.Helper()
	data, err := json.Marshal(DueChanged{Assessment: submissionID, Classroom: submissionID, Org: "org", DueAt: due})
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: topicDueChanged, Resource: submissionID, Timestamp: at, Data: data}
}

func late(t *testing.T, db *pgxpool.Pool, id string) bool {
	t.Helper()
	var v bool
	if err := db.QueryRow(t.Context(), "SELECT late FROM evaluations WHERE id=$1", id).Scan(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

// apply due-date changes to current and late-arriving evaluations; ignore stale events.
func TestLateFlagsFollowTheDueDate(t *testing.T) {
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted, "late": true})
	later, earlier := submitted.Add(24*time.Hour), submitted.Add(-time.Hour)
	changed := submitted.Add(time.Hour)
	for range 2 {
		if err := apply(t, db, changeDue, dueChange(t, &later, changed)); err != nil {
			t.Fatal(err)
		}
	}
	if late(t, db, submissionA) {
		t.Fatal("the extended due date left the work late")
	}
	if err := apply(t, db, changeDue, dueChange(t, &earlier, changed.Add(-time.Minute))); err != nil {
		t.Fatal(err)
	}
	if late(t, db, submissionA) {
		t.Fatal("an older change was applied")
	}
	// submitted before the extension, its event arriving after it: late against the old due date, not the new
	// one.
	submit(t, db, submissionB, "student-b", map[string]any{"submitted_at": submitted, "late": true})
	if late(t, db, submissionB) {
		t.Fatal("a submission graded after the change kept the old late flag")
	}
	if err := apply(t, db, changeDue, dueChange(t, &earlier, changed.Add(time.Minute))); err != nil {
		t.Fatal(err)
	}
	if !late(t, db, submissionA) || !late(t, db, submissionB) {
		t.Fatal("work after an earlier due date is not late")
	}
	if err := apply(t, db, changeDue, dueChange(t, nil, changed.Add(2*time.Minute))); err != nil {
		t.Fatal(err)
	}
	if late(t, db, submissionA) || late(t, db, submissionB) {
		t.Fatal("work with no due date is late")
	}
	if err := purgeAssessmentNow(t, db, deletion(t)); err != nil {
		t.Fatal(err)
	}
	if err := apply(t, db, changeDue, dueChange(t, &later, changed.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	if n := count(t, db, "SELECT count(*) FROM assessment_due"); n != 0 {
		t.Fatalf("a deleted assessment kept %d due dates", n)
	}
}

// account deletion removes only that student's grading data and blocks replay.
func TestADeletedAccountLeavesNoGrades(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	gone := "b1b2c3d4-0000-4000-8000-0000000000aa"
	submit(t, db, submissionA, gone, map[string]any{"ai_marking": false, "release_policy": "on_finalize"})
	submit(t, db, submissionB, gone, map[string]any{"assessment_id": submissionB}) // ai marking queues a job
	submit(t, db, submissionC, "student-c", map[string]any{"ai_marking": false})
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	data, err := json.Marshal(platform.UserDeleted{User: gone, Org: "org", Role: "student"})
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err = apply(t, db, purgeUser, platform.Event{Type: platform.TopicUserDeleted, Resource: gone, Data: data}); err != nil {
			t.Fatal(err)
		}
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE student_id=$1", gone); n != 0 {
		t.Fatalf("%d evaluations left", n)
	}
	if events(t, db, submissionA) != 0 || audits(t, db, submissionA) != 0 || jobs(t, db) != 0 {
		t.Fatal("the deleted account's events, audit or marking jobs were kept")
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE student_id='student-c'"); n != 1 {
		t.Fatalf("another student lost their grade: %d", n)
	}
	late := "a1b2c3d4-0000-4000-8000-0000000000dd"
	submit(t, db, late, gone, map[string]any{"assessment_id": late})
	if n := count(t, db, "SELECT count(*) FROM evaluations WHERE id=$1", late); n != 0 {
		t.Fatal("a late submission event graded the deleted account")
	}
}

func TestAReleaseShowsOnlyEnrolledStudentsGrades(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", autoMarked)
	submit(t, db, submissionB, "student-b", autoMarked)
	for _, id := range []string{submissionA, submissionB} {
		if _, err := s.finalizeEvaluation(ctx, "org", id, "teacher-1", "Checked", nil); err != nil {
			t.Fatal(err)
		}
	}
	// student-a has been removed from the classroom: their grade is not counted, so it is not released either.
	enrolled := []string{"student-b"}
	if c, err := s.countEvaluations(ctx, "org", submissionID, submissionID, enrolled); err != nil || c.Unreleased != 1 {
		t.Fatalf("counts %+v, %v", c, err)
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", enrolled); err != nil || n != 1 {
		t.Fatalf("release: %d %v", n, err)
	}
	if a := load(t, db, submissionA); a.status != statusFinalized || a.released != nil || events(t, db, submissionA) != 0 {
		t.Fatalf("a removed student's grade was released: %+v", a)
	}
	if load(t, db, submissionB).released == nil || events(t, db, submissionB) != 1 {
		t.Fatal("the enrolled student's grade was not released")
	}
	// enrolled again, their kept grade is released at the next release.
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", everyone); err != nil || n != 1 || load(t, db, submissionA).released == nil {
		t.Fatalf("release once enrolled again: %d %v", n, err)
	}
}

func TestTheTeachersListComesInPagesThatNeverOverlap(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	at := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	for i, id := range []string{submissionA, submissionB, submissionC} {
		submit(t, db, id, everyone[i], map[string]any{"submitted_at": at})
	}
	// the same creation time for every one: the id orders them.
	if _, err := db.Exec(ctx, "UPDATE evaluations SET created_at=$1", at); err != nil {
		t.Fatal(err)
	}
	read := func(assessment string, offset int) []string {
		t.Helper()
		page, total, err := s.listEvaluations(ctx, "org", submissionID, assessment, everyone, offset, 2)
		if err != nil || total != 3 {
			t.Fatalf("%q at %d: %d in all, %v", assessment, offset, total, err)
		}
		ids := []string{}
		for _, v := range page {
			ids = append(ids, v.ID)
		}
		return ids
	}
	if got := append(read(submissionID, 0), read(submissionID, 2)...); strings.Join(got, ",") != strings.Join([]string{submissionA, submissionB, submissionC}, ",") {
		t.Fatalf("one assessment's pages, oldest first: %v", got)
	}
	if got := append(read("", 0), read("", 2)...); strings.Join(got, ",") != strings.Join([]string{submissionC, submissionB, submissionA}, ",") {
		t.Fatalf("the classroom's pages, newest first: %v", got)
	}
	if page, total, err := s.listEvaluations(ctx, "org", submissionID, submissionC, everyone, 0, 2); err != nil || total != 0 || len(page) != 0 {
		t.Fatalf("another assessment: %v of %d, %v", page, total, err)
	}
	if v, err := s.findEvaluation(ctx, "org", submissionB); err != nil || v.Student != "student-b" {
		t.Fatalf("find: %+v %v", v, err)
	}
	if _, err := s.findEvaluation(ctx, "another-org", submissionB); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("found in another organization: %v", err)
	}
}

// reassignment is identity.classroom.reassigned.v1 giving the test submissions' classroom to teacher at at.
func reassignment(t *testing.T, teacher string, at time.Time) platform.Event {
	t.Helper()
	data, err := json.Marshal(platform.ClassroomReassigned{Classroom: submissionID, Org: "org", Teacher: teacher})
	if err != nil {
		t.Fatal(err)
	}
	return platform.Event{Type: platform.TopicClassroomReassigned, Resource: submissionID, Timestamp: at, Data: data}
}

func payer(t *testing.T, db *pgxpool.Pool, id string) *string {
	t.Helper()
	var teacher *string
	if err := db.QueryRow(t.Context(), "SELECT teacher_id FROM evaluations WHERE id=$1", id).Scan(&teacher); err != nil {
		t.Fatal(err)
	}
	return teacher
}

func TestAReassignedClassroomsNewTeacherPaysForItsMarking(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", map[string]any{"teacher_id": "teacher-old"})
	submit(t, db, submissionC, "student-c", map[string]any{"teacher_id": "teacher-old", "ai_marking": false})
	at := time.Date(2026, 11, 19, 9, 0, 0, 0, time.UTC)
	// a duplicate changes nothing, and an older reassignment delivered late does not win.
	for _, event := range []platform.Event{reassignment(t, "teacher-new", at), reassignment(t, "teacher-new", at), reassignment(t, "teacher-stale", at.Add(-time.Hour))} {
		if err := apply(t, db, reassign, event); err != nil {
			t.Fatal(err)
		}
	}
	if p := payer(t, db, submissionA); p == nil || *p != "teacher-new" {
		t.Fatalf("a draft's payer = %v", p)
	}
	if p := payer(t, db, submissionC); p != nil {
		t.Fatalf("work without ai marking gained a payer: %s", *p)
	}
	// a submission the assessment's teacher made before the reassignment, arriving after it.
	submit(t, db, submissionB, "student-b", map[string]any{"teacher_id": "teacher-old"})
	if p := payer(t, db, submissionB); p == nil || *p != "teacher-new" {
		t.Fatalf("a late submission's payer = %v", p)
	}
	f := newFakeAI(t, `{"output":null,"model_version":"m"}`)
	markOnce(t, marker{db: db, ai: f.client()})
	if f.body["billed_to"].(map[string]any)["user_id"] != "teacher-new" {
		t.Fatalf("billed %v", f.body["billed_to"])
	}
	// a purged classroom forgets its teacher and is not recorded again by a reassignment that arrives later.
	deleted, err := json.Marshal(platform.ClassroomDeleted{Classroom: submissionID, Org: "org"})
	if err != nil {
		t.Fatal(err)
	}
	if err = apply(t, db, purge, platform.Event{Type: platform.TopicClassroomDeleted, Resource: submissionID, Data: deleted}); err != nil {
		t.Fatal(err)
	}
	if err = apply(t, db, reassign, reassignment(t, "teacher-later", at.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	if n := count(t, db, "SELECT count(*) FROM classroom_teachers"); n != 0 {
		t.Fatalf("%d classroom teachers after the purge", n)
	}
	var invalid platform.InvalidEvent
	if err = apply(t, db, reassign, platform.Event{Resource: submissionID, Timestamp: at, Data: json.RawMessage(`{}`)}); !errors.As(err, &invalid) {
		t.Fatalf("a malformed reassignment: %v", err)
	}
}

// a grade finalized before its classroom was reassigned keeps its payer until it is reopened, when the new
// teacher pays for any marking of the draft.
func TestAReopenedGradeOfAReassignedClassroomBillsItsNewTeacher(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", map[string]any{"teacher_id": "teacher-old"})
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-old", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	if err := apply(t, db, reassign, reassignment(t, "teacher-new", time.Date(2026, 11, 19, 9, 0, 0, 0, time.UTC))); err != nil {
		t.Fatal(err)
	}
	if p := payer(t, db, submissionA); p == nil || *p != "teacher-old" {
		t.Fatalf("a finalized grade's payer = %v", p)
	}
	if _, err := s.reopenEvaluation(ctx, "org", submissionA, "teacher-new", "Mis-marked"); err != nil {
		t.Fatal(err)
	}
	if p := payer(t, db, submissionA); p == nil || *p != "teacher-new" {
		t.Fatalf("a reopened grade's payer = %v", p)
	}
}

// a return finds the student's drafts, an account's purge its evaluations, and purges their audit entries,
// through the indexes grading builds beside its work.
func TestGradingFindsItsRowsThroughTheIndexesItBuilds(t *testing.T) {
	db := testDB(t)
	ctx := t.Context()
	if err := platform.BuildIndexes(ctx, db, indexes()); err != nil {
		t.Fatal(err)
	}
	tx, err := db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SET LOCAL enable_seqscan=off"); err != nil {
		t.Fatal(err)
	}
	classroom := "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"
	// a student's drafts in a classroom may come through either index that leads with one of the two.
	for _, c := range []struct {
		sql     string
		indexes []string
		args    []any
	}{
		{draftsOfStudent, []string{"evaluations_classroom_student", "evaluations_student"}, []any{classroom, "s", "o", statusDraft}},
		{"SELECT 1 FROM evaluations WHERE student_id=$1 AND org_id=$2 FOR UPDATE", []string{"evaluations_student"}, []any{"s", "o"}},
		{"SELECT 1 FROM evaluations WHERE classroom_id=$1 AND org_id=$2 FOR UPDATE", []string{"evaluations_classroom_student"}, []any{classroom, "o"}},
		{"DELETE FROM audit WHERE evaluation_id IN (SELECT id FROM evaluations WHERE classroom_id=$1 AND org_id=$2)", []string{"audit_evaluation"}, []any{classroom, "o"}},
	} {
		rows, err := tx.Query(ctx, "EXPLAIN "+c.sql, c.args...)
		var plan []string
		if err == nil {
			plan, err = pgx.CollectRows(rows, pgx.RowTo[string])
		}
		indexed := slices.ContainsFunc(plan, func(line string) bool {
			return slices.ContainsFunc(c.indexes, func(index string) bool { return strings.Contains(line, index) })
		})
		if err != nil || !indexed || slices.ContainsFunc(plan, func(line string) bool { return strings.Contains(line, "Seq Scan") }) {
			t.Fatalf("%s: plan %v, %v", c.sql, plan, err)
		}
	}
}

// withCritic is a marker that checks every ready suggestion a second time (adr 0020).
func withCritic(db *pgxpool.Pool, ai suggester) marker {
	return marker{db: db, ai: ai, markingCritic: true}
}

func stage(t *testing.T, db *pgxpool.Pool) string {
	t.Helper()
	var s string
	if err := db.QueryRow(t.Context(), "SELECT stage FROM marking_jobs").Scan(&s); err != nil {
		t.Fatal(err)
	}
	return s
}

func encoded(t *testing.T, v any) string {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

// a ready suggestion becomes a review job, whose second, blind marking is stored beside it as agreed or
// doubtful; neither changes the suggestion, a mark or the audit.
func TestTheCriticChecksAReadySuggestion(t *testing.T) {
	disagree := reply(t, []map[string]any{criterion("c1", 0)}, "Critic feedback.")
	for _, tc := range []struct {
		name, review, status string
		reasons              []string
	}{
		{"agreed", grounded3(t), reviewAgreed, nil},
		{"doubtful", disagree, reviewDoubtful, []string{doubtLevelsDiffer}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := testDB(t)
			submit(t, db, submissionA, "student-a", map[string]any{"teacher_id": "teacher-9"})
			ai := &fakeSuggester{output: grounded3(t), review: tc.review}
			m := withCritic(db, ai)
			markOnce(t, m)
			e := load(t, db, submissionA).evidence[0]
			if e.Suggestion.Status != suggestionReady || e.Suggestion.Review == nil || e.Suggestion.Review.Status != reviewPending {
				t.Fatalf("no review pending: %+v", e.Suggestion)
			}
			var attempts int
			if err := db.QueryRow(t.Context(), "SELECT attempts FROM marking_jobs WHERE stage='review'").Scan(&attempts); err != nil || attempts != 0 {
				t.Fatalf("no fresh review job: %d %v", attempts, err)
			}
			markOnce(t, m)
			e = load(t, db, submissionA).evidence[0]
			r := e.Suggestion.Review
			if r == nil || r.Status != tc.status || !slices.Equal(r.Reasons, tc.reasons) || r.Model != "model-2" || len(r.Criteria) != 1 {
				t.Fatalf("%+v", r)
			}
			if e.Suggestion.Awarded != 3 || e.Suggestion.Feedback != "Say more." || e.Awarded != 0 || !e.NeedsMarking || e.MarkedBy != "" {
				t.Fatalf("the review changed the suggestion or the mark: %+v %+v", e, e.Suggestion)
			}
			if strings.Contains(encoded(t, e), "Critic feedback.") {
				t.Fatal("the critic's feedback was kept")
			}
			if jobs(t, db) != 0 || audits(t, db, submissionA) != 0 || ai.calls != 2 {
				t.Fatalf("jobs %d, audits %d, calls %d", jobs(t, db), audits(t, db, submissionA), ai.calls)
			}
			if !slices.Equal(ai.names, []string{schemaSuggestion, schemaReview}) || ai.who != (platform.Billed{UserID: "teacher-9", OrgID: "org", Role: "teacher"}) {
				t.Fatalf("calls %v billed %+v", ai.names, ai.who)
			}
			// blind: the critic marks the same task under its own instructions, never shown the suggestion.
			if ai.messages[1][0].Content != criticInstructions || ai.messages[1][1] != ai.messages[0][1] {
				t.Fatal("the critic did not mark the same task under its own instructions")
			}
			for _, msg := range ai.messages[1] {
				if strings.Contains(msg.Content, "Say more.") || strings.Contains(msg.Content, "model-1") {
					t.Fatalf("the critic saw the suggestion: %s", msg.Content)
				}
			}
		})
	}
}

// a second marking that breaks the rules, or a call that fails, leaves the suggestion not reviewed: never agreed.
func TestACriticFailureIsNotReviewed(t *testing.T) {
	for _, tc := range []struct {
		name, review, reason string
		err                  error
	}{
		{"ungrounded", reply(t, []map[string]any{criterion("c1", 3, "a mutex")}, ""), "UNGROUNDED", nil},
		{"invalid", "Agreed!", "INVALID_OUTPUT", nil},
		{"rejected", "", "AI_OUTPUT_REJECTED", &platform.AIError{Status: 502, Code: "AI_OUTPUT_REJECTED"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := testDB(t)
			submit(t, db, submissionA, "student-a", nil)
			ai := &fakeSuggester{output: grounded3(t)}
			m := withCritic(db, ai)
			markOnce(t, m)
			ai.review, ai.err = tc.review, tc.err
			markOnce(t, m)
			s := load(t, db, submissionA).evidence[0].Suggestion
			if s.Status != suggestionReady || s.Awarded != 3 || s.Review == nil || s.Review.Status != reviewNotReviewed || s.Review.Reason != tc.reason || jobs(t, db) != 0 {
				t.Fatalf("%+v %+v", s, s.Review)
			}
		})
	}
}

// the teacher's spend limit holds the review without using an attempt, and leaves the suggestion as it is.
func TestTheCriticWaitsForTheSpendLimit(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	m := withCritic(db, ai)
	markOnce(t, m)
	resets := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	ai.err = &platform.AIError{Status: 429, Code: "AI_SPEND_LIMIT", ResetsAt: resets}
	markOnce(t, m)
	var attempts int
	var after time.Time
	if err := db.QueryRow(t.Context(), "SELECT attempts,run_after FROM marking_jobs WHERE stage='review'").Scan(&attempts, &after); err != nil {
		t.Fatal(err)
	}
	s := load(t, db, submissionA).evidence[0].Suggestion
	if attempts != 0 || !after.Equal(resets) || s.Status != suggestionReady || s.Awarded != 3 || s.Review.Status != reviewPending || s.Review.Reason != "AI_SPEND_LIMIT" {
		t.Fatalf("attempts %d, run after %v, %+v %+v", attempts, after, s, s.Review)
	}
}

// finalizing ends a review still waiting, so a reopened grade never shows a second check that will not run.
func TestFinalizingEndsAWaitingReview(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
	markOnce(t, withCritic(db, ai))
	if _, err := (pgStore{db}).finalizeEvaluation(t.Context(), "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	r := load(t, db, submissionA).evidence[0].Suggestion.Review
	if r == nil || r.Status != reviewNotReviewed || r.Reason != reasonFinalized || jobs(t, db) != 0 || ai.calls != 1 {
		t.Fatalf("%+v, jobs %d, calls %d", r, jobs(t, db), ai.calls)
	}
}

// a teacher who saved the suggestion's own marks still has them checked; one who marked otherwise does not.
func TestATeacherWhoTookTheSuggestionStillHasItChecked(t *testing.T) {
	for _, tc := range []struct {
		name           string
		points         int
		status, reason string
		calls          int
	}{
		{"took it", 3, reviewAgreed, "", 2},
		{"marked otherwise", 0, reviewNotReviewed, "MARKED_BY_TEACHER", 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			db := testDB(t)
			submit(t, db, submissionA, "student-a", nil)
			ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
			m := withCritic(db, ai)
			markOnce(t, m)
			if _, err := (pgStore{db}).saveMarks(t.Context(), "org", submissionA, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": tc.points}}}); err != nil {
				t.Fatal(err)
			}
			markOnce(t, m)
			e := load(t, db, submissionA).evidence[0]
			r := e.Suggestion.Review
			if r == nil || r.Status != tc.status || r.Reason != tc.reason || ai.calls != tc.calls || e.Awarded != tc.points || e.MarkedBy != markedTeacher {
				t.Fatalf("%+v %+v, calls %d", e, r, ai.calls)
			}
		})
	}
}

// a grade finalized while the critic marks ends the review, and the late review never reaches the final grade.
func TestAReviewAfterFinalizeIsDropped(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
	m := withCritic(db, ai)
	markOnce(t, m)
	ai.during = func() {
		if _, err := (pgStore{db}).finalizeEvaluation(context.Background(), "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 0}}}); err != nil {
			t.Error(err)
		}
	}
	markOnce(t, m)
	after := load(t, db, submissionA)
	if after.status != statusFinalized || after.evidence[0].Awarded != 0 || after.evidence[0].Suggestion.Review.Reason != reasonFinalized || jobs(t, db) != 0 || audits(t, db, submissionA) != 1 {
		t.Fatalf("a late review reached a final grade: %+v", after.evidence[0].Suggestion.Review)
	}
}

// a review of a suggestion replaced while the critic marked is discarded.
func TestAReviewOfAReplacedSuggestionIsDiscarded(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
	m := withCritic(db, ai)
	markOnce(t, m)
	other := Suggestion{Status: suggestionReady, Criteria: []SuggestedCriterion{{Criterion: "c1", Points: 0, Quotes: []string{}, Rationale: "Other."}}, Model: "model-1"}
	ai.during = func() {
		if _, err := db.Exec(context.Background(), `UPDATE evaluations SET evidence=jsonb_set(evidence,'{0,suggestion}',$1::jsonb) WHERE id=$2`, encoded(t, other), submissionA); err != nil {
			t.Error(err)
		}
	}
	markOnce(t, m)
	s := load(t, db, submissionA).evidence[0].Suggestion
	if s.Awarded != 0 || s.Review != nil || jobs(t, db) != 0 {
		t.Fatalf("a review reached another suggestion: %+v %+v", s, s.Review)
	}
}

// with the critic off a ready suggestion ends its job and has no review; a review job queued before it was
// turned off ends not reviewed, without an ai call.
func TestTheCriticOffQueuesNoReview(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t)}
	markOnce(t, marker{db: db, ai: ai})
	if s := load(t, db, submissionA).evidence[0].Suggestion; s.Status != suggestionReady || s.Review != nil || jobs(t, db) != 0 {
		t.Fatalf("%+v, jobs %d", s, jobs(t, db))
	}
	db = testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	ai = &fakeSuggester{output: grounded3(t)}
	markOnce(t, withCritic(db, ai))
	if stage(t, db) != stageReview {
		t.Fatal("no review job")
	}
	markOnce(t, marker{db: db, ai: ai})
	if s := load(t, db, submissionA).evidence[0].Suggestion; ai.calls != 1 || s.Review.Status != reviewNotReviewed || s.Review.Reason != "CRITIC_OFF" || jobs(t, db) != 0 {
		t.Fatalf("calls %d, %+v", ai.calls, s.Review)
	}
}

// a repeated assessment.submitted still gets one suggestion and one review.
func TestADuplicateSubmissionIsReviewedOnce(t *testing.T) {
	db := testDB(t)
	submit(t, db, submissionA, "student-a", nil)
	submit(t, db, submissionA, "student-a", nil)
	ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
	m := withCritic(db, ai)
	markOnce(t, m)
	submit(t, db, submissionA, "student-a", nil) // redelivered while the review waits
	markOnce(t, m)
	submit(t, db, submissionA, "student-a", nil)
	if _, found, err := m.lease(t.Context()); err != nil || found {
		t.Fatalf("another job: %v %v", found, err)
	}
	s := load(t, db, submissionA).evidence[0].Suggestion
	if ai.calls != 2 || !slices.Equal(ai.names, []string{schemaSuggestion, schemaReview}) || s.Review.Status != reviewAgreed {
		t.Fatalf("calls %v, %+v", ai.names, s.Review)
	}
	if n := count(t, db, "SELECT count(*) FROM evaluations"); n != 1 {
		t.Fatalf("%d evaluations", n)
	}
}

// a review stopped while its student was out is checked again on their return, keeping the suggestion.
func TestAReturnQueuesAgainAReviewStoppedByARemoval(t *testing.T) {
	db := testDB(t)
	submitted := time.Date(2026, 10, 1, 9, 0, 0, 0, time.UTC)
	submit(t, db, submissionA, "student-a", map[string]any{"submitted_at": submitted})
	ai := &fakeSuggester{output: grounded3(t), review: grounded3(t)}
	m := withCritic(db, ai)
	markOnce(t, m)
	if err := apply(t, db, removeStudent, removal(t, submitted.Add(time.Hour))); err != nil {
		t.Fatal(err)
	}
	markOnce(t, m)
	if s := load(t, db, submissionA).evidence[0].Suggestion; ai.calls != 1 || s.Review.Reason != reasonStudentRemoved || jobs(t, db) != 0 {
		t.Fatalf("calls %d, %+v", ai.calls, s.Review)
	}
	if err := apply(t, db, restoreStudent, enrollment(t, submitted.Add(2*time.Hour))); err != nil {
		t.Fatal(err)
	}
	if stage(t, db) != stageReview {
		t.Fatal("the review was not queued again")
	}
	markOnce(t, m)
	if s := load(t, db, submissionA).evidence[0].Suggestion; ai.calls != 2 || s.Status != suggestionReady || s.Awarded != 3 || s.Review.Status != reviewAgreed {
		t.Fatalf("calls %d, %+v %+v", ai.calls, s, s.Review)
	}
}

// the counts say how many of the drafts a bulk finalize would finalize have a doubtful suggestion.
func TestTheCountsNameReadyDraftsWithDoubts(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", nil)
	submit(t, db, submissionB, "student-b", nil)
	ai := &fakeSuggester{output: grounded3(t), review: reply(t, []map[string]any{criterion("c1", 0)}, "")}
	m := withCritic(db, ai)
	for range 4 {
		markOnce(t, m)
	}
	for _, id := range []string{submissionA, submissionB} {
		if _, err := s.saveMarks(ctx, "org", id, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(ctx, `UPDATE evaluations SET evidence=jsonb_set(evidence,'{0,suggestion,review,status}','"agreed"') WHERE id=$1`, submissionB); err != nil {
		t.Fatal(err)
	}
	c, err := s.countEvaluations(ctx, "org", submissionID, submissionID, everyone)
	if err != nil || c.Ready != 2 || c.ReadyDoubtful != 1 {
		t.Fatalf("%+v %v", c, err)
	}
	// once the teacher marks the doubted question otherwise, the doubt is settled.
	if _, err = s.saveMarks(ctx, "org", submissionA, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 0}}}); err != nil {
		t.Fatal(err)
	}
	if c, err = s.countEvaluations(ctx, "org", submissionID, submissionID, everyone); err != nil || c.Ready != 2 || c.ReadyDoubtful != 0 {
		t.Fatalf("%+v %v", c, err)
	}
}
