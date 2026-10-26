package main

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// these run against a real postgresql like store_test.go (adr 0016).

func reopenedEvents(t *testing.T, db *pgxpool.Pool, id string) int {
	return count(t, db, "SELECT count(*) FROM outbox WHERE topic=$1 AND partition_key=$2", topicReopened, id)
}

// newestRevision is the revision the evaluation's newest outbox event of topic carries.
func newestRevision(t *testing.T, db *pgxpool.Pool, topic, id string) int {
	t.Helper()
	var body []byte
	if err := db.QueryRow(t.Context(), "SELECT body FROM outbox WHERE topic=$1 AND partition_key=$2 ORDER BY created_at DESC LIMIT 1", topic, id).Scan(&body); err != nil {
		t.Fatal(err)
	}
	var event struct {
		Data struct {
			Revision int `json:"revision"`
		} `json:"data"`
	}
	if err := json.Unmarshal(body, &event); err != nil {
		t.Fatal(err)
	}
	return event.Data.Revision
}

func TestAReopenedGradeIsADraftUntilFinalizedAgain(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", map[string]any{"ai_marking": false, "release_policy": "on_finalize"})
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 3}}}); err != nil {
		t.Fatal(err)
	}
	if newestRevision(t, db, topicFinalized, submissionA) != 1 {
		t.Fatal("the first finalization is not revision 1")
	}
	v, err := s.reopenEvaluation(ctx, "org", submissionA, "teacher-1", "Mis-marked the reasoning")
	if err != nil {
		t.Fatal(err)
	}
	if r := load(t, db, submissionA); v.Status != statusDraft || v.Revision != 2 || r.status != statusDraft || r.released != nil || r.evidence[0].Awarded != 3 {
		t.Fatalf("reopened: %+v %+v", v, r)
	}
	if count(t, db, "SELECT count(*) FROM audit WHERE evaluation_id=$1 AND action='reopen' AND reason='Mis-marked the reasoning'", submissionA) != 1 {
		t.Fatal("the reopen was not audited")
	}
	if reopenedEvents(t, db, submissionA) != 1 || newestRevision(t, db, topicReopened, submissionA) != 2 {
		t.Fatal("a released grade's reopen must be published once at its new revision")
	}
	if mine, err := s.listReleased(ctx, "org", submissionID, "student-a"); err != nil || len(mine) != 0 {
		t.Fatalf("the student saw a reopened grade: %+v %v", mine, err)
	}
	if _, err = s.reopenEvaluation(ctx, "org", submissionA, "teacher-1", "Again"); !errors.Is(err, errNotFinalized) {
		t.Fatalf("reopened a draft: %v", err)
	}
	if _, err = s.saveMarks(ctx, "org", submissionA, "teacher-1", map[string]Mark{questionID: {Criteria: map[string]int{"c1": 0}}}); err != nil {
		t.Fatal(err)
	}
	if _, err = s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Corrected", nil); err != nil {
		t.Fatal(err)
	}
	if events(t, db, submissionA) != 2 || newestRevision(t, db, topicFinalized, submissionA) != 2 || load(t, db, submissionA).evidence[0].Awarded != 0 {
		t.Fatal("the corrected grade must be published at revision 2")
	}
	if mine, err := s.listReleased(ctx, "org", submissionID, "student-a"); err != nil || len(mine) != 1 || mine[0].Evidence[0].Awarded != 0 {
		t.Fatalf("the student does not see the corrected grade: %+v %v", mine, err)
	}
}

func TestReopeningAnUnreleasedGradePublishesNothing(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	submit(t, db, submissionA, "student-a", autoMarked)
	if _, err := s.finalizeEvaluation(ctx, "org", submissionA, "teacher-1", "Checked", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := s.reopenEvaluation(ctx, "org", submissionA, "teacher-1", "Wrong key"); err != nil {
		t.Fatal(err)
	}
	if reopenedEvents(t, db, submissionA) != 0 || events(t, db, submissionA) != 0 {
		t.Fatal("analytics never had this grade, so its reopen is not published")
	}
	if n, err := s.release(ctx, "org", submissionID, "teacher-1", everyone); err != nil || n != 0 {
		t.Fatalf("a release released a reopened draft: %d %v", n, err)
	}
}

func TestABulkFinalizeTakesOnlyEnrolledFullyMarkedDrafts(t *testing.T) {
	db := testDB(t)
	ctx, s := t.Context(), pgStore{db}
	onFinalize := map[string]any{"release_policy": "on_finalize"}
	for k, v := range autoMarked {
		onFinalize[k] = v
	}
	submit(t, db, submissionA, "student-a", onFinalize)
	submit(t, db, submissionB, "student-b", map[string]any{"ai_marking": false})
	submit(t, db, submissionC, "student-c", onFinalize)
	roster := []string{"student-a", "student-b"}
	if c, err := s.countEvaluations(ctx, "org", submissionID, submissionID, roster); err != nil || c.Drafts != 2 || c.Ready != 1 {
		t.Fatalf("counts: %+v %v", c, err)
	}
	finalized, waiting, err := s.finalizeAssessment(ctx, "org", submissionID, "teacher-1", bulkReason, roster)
	if err != nil || finalized != 1 || waiting != 1 {
		t.Fatalf("finalized %d, waiting %d, %v", finalized, waiting, err)
	}
	if load(t, db, submissionA).status != statusFinalized || events(t, db, submissionA) != 1 || count(t, db, "SELECT count(*) FROM audit WHERE evaluation_id=$1 AND action='finalize' AND reason=$2", submissionA, bulkReason) != 1 {
		t.Fatal("the marked draft was not finalized, audited and published once")
	}
	if load(t, db, submissionB).status != statusDraft || load(t, db, submissionC).status != statusDraft || events(t, db, submissionC) != 0 {
		t.Fatal("a draft needing marking or of a removed student was finalized")
	}
	if finalized, waiting, err = s.finalizeAssessment(ctx, "org", submissionID, "teacher-1", bulkReason, roster); err != nil || finalized != 0 || waiting != 1 || events(t, db, submissionA) != 1 {
		t.Fatalf("a repeated bulk finalize: %d %d %v", finalized, waiting, err)
	}
}
