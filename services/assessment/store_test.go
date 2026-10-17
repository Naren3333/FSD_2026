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

	"github.com/google/uuid"
)

// These tests run pgStore against real PostgreSQL in a throwaway schema. They need ASSESSMENT_TEST_DATABASE_URL
// and are skipped without it.

func testStore(t *testing.T) pgStore {
	return pgStore{platformtest.DB(t, "ASSESSMENT_TEST_DATABASE_URL", migrations)}
}

// stored inserts an assessment in org's classroom room with the given status and opening date.
func stored(t *testing.T, p pgStore, org, status string, opens *time.Time) Assessment {
	t.Helper()
	v := fixture()
	v.ID, v.Questions[0].ID, v.Classroom, v.Status, v.OpensAt, v.CreatedBy = uuid.NewString(), uuid.NewString(), room, status, opens, "teacher-1"
	v.Questions[0].Points = 3
	if err := p.insertAssessment(context.Background(), org, v); err != nil {
		t.Fatal(err)
	}
	return v
}

func count(t *testing.T, p pgStore, sql string, args ...any) int {
	t.Helper()
	var n int
	if err := p.db.QueryRow(context.Background(), sql, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestTheStoreKeepsOrganizationsApart(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	draft := stored(t, p, "org-a", statusDraft, nil)
	published := stored(t, p, "org-a", statusPublished, nil)
	if _, err := p.findAssessment(ctx, "org-b", draft.ID); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("found another organization's assessment: %v", err)
	}
	changed := draft
	changed.Title = "Taken"
	for name, err := range map[string]error{
		"update":  p.updateDraft(ctx, "org-b", changed),
		"publish": p.publishDraft(ctx, "org-b", draft.ID, time.Now()),
		"delete":  p.deleteDraft(ctx, "org-b", draft.ID),
	} {
		if !errors.Is(err, platform.ErrNotFound) {
			t.Errorf("%s in another organization: %v", name, err)
		}
	}
	got, err := p.findAssessment(ctx, "org-a", draft.ID)
	if err != nil || got.Title != draft.Title || got.Status != statusDraft || got.CreatedBy != "teacher-1" || got.Questions[0].Points != 3 {
		t.Fatalf("%+v %v", got, err)
	}
	if err = p.updateDraft(ctx, "org-a", published); !errors.Is(err, errNotDraft) {
		t.Fatalf("updated a published assessment: %v", err)
	}
	for org, want := range map[string]int{"org-a": 2, "org-b": 0} {
		list, err := p.listAssessments(ctx, org, room, "s", true, time.Now())
		progress, err2 := p.listProgress(ctx, org, room)
		if err != nil || err2 != nil || len(list) != want || len(progress) != want {
			t.Errorf("%s: %d listed, %d in progress (%v %v)", org, len(list), len(progress), err, err2)
		}
	}
}

func TestADraftChangesOnlyWhileADraft(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusDraft, nil)
	v.Title = "Edited"
	if err := p.updateDraft(ctx, "org", v); err != nil {
		t.Fatal(err)
	}
	at := time.Now().UTC().Truncate(time.Microsecond)
	if err := p.publishDraft(ctx, "org", v.ID, at); err != nil {
		t.Fatal(err)
	}
	got, err := p.findAssessment(ctx, "org", v.ID)
	if err != nil || got.Title != "Edited" || got.Status != statusPublished || got.PublishedAt == nil || !got.PublishedAt.Equal(at) {
		t.Fatalf("%+v %v", got, err)
	}
	for name, err := range map[string]error{"publish": p.publishDraft(ctx, "org", v.ID, at), "delete": p.deleteDraft(ctx, "org", v.ID)} {
		if !errors.Is(err, errNotDraft) {
			t.Errorf("%s once published: %v", name, err)
		}
	}
	other := stored(t, p, "org", statusDraft, nil)
	if err = p.deleteDraft(ctx, "org", other.ID); err != nil || count(t, p, "SELECT count(*) FROM assessments") != 1 {
		t.Fatalf("delete: %v", err)
	}
}

func TestStudentsListOnlyWhatIsOpen(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	now := time.Now()
	past, future := now.Add(-time.Hour), now.Add(time.Hour)
	open := stored(t, p, "org", statusPublished, nil)
	opened := stored(t, p, "org", statusPublished, &past)
	stored(t, p, "org", statusPublished, &future)
	stored(t, p, "org", statusDraft, nil)
	elsewhere := fixture()
	elsewhere.ID, elsewhere.Classroom = uuid.NewString(), skill
	if err := p.insertAssessment(ctx, "org", elsewhere); err != nil {
		t.Fatal(err)
	}
	if _, err := p.insertSubmission(ctx, open, Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{"q": "2"}, SubmittedAt: now}); err != nil {
		t.Fatal(err)
	}
	for all, want := range map[bool]int{false: 2, true: 4} {
		list, err := p.listAssessments(ctx, "org", room, "s", all, now)
		if err != nil || len(list) != want {
			t.Fatalf("all=%v: %+v %v", all, list, err)
		}
		if all {
			continue
		}
		for _, v := range list {
			if v.Status != statusPublished || v.Questions != 1 || v.Points != 3 || v.Submitted != (v.ID == open.ID) || (v.ID != open.ID && v.ID != opened.ID) {
				t.Errorf("%+v", v)
			}
		}
	}
	progress, err := p.listProgress(ctx, "org", room)
	if err != nil || len(progress) != 4 {
		t.Fatalf("%+v %v", progress, err)
	}
	for _, v := range progress {
		if v.Submissions != map[bool]int{true: 1, false: 0}[v.ID == open.ID] {
			t.Errorf("%+v", v)
		}
	}
}

func TestOneSubmissionOneEvent(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	if _, err := p.saveAnswers(ctx, v.ID, "s", map[string]string{v.Questions[0].ID: "1"}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	inserted := make([]bool, 2)
	errs := make([]error, 2)
	for i := range 2 {
		wg.Go(func() {
			sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()}
			inserted[i], errs[i] = p.insertSubmission(ctx, v, sub)
		})
	}
	wg.Wait()
	if errs[0] != nil || errs[1] != nil || inserted[0] == inserted[1] {
		t.Fatalf("inserted %v, errors %v", inserted, errs)
	}
	if n := count(t, p, "SELECT count(*) FROM submissions WHERE assessment_id=$1", v.ID); n != 1 {
		t.Fatalf("%d submissions", n)
	}
	if n := count(t, p, "SELECT count(*) FROM outbox WHERE topic=$1", topicSubmitted); n != 1 {
		t.Fatalf("%d events", n)
	}
	if n := count(t, p, "SELECT count(*) FROM saved_answers"); n != 0 {
		t.Fatalf("saved work kept after submitting: %d", n)
	}
	if _, err := p.saveAnswers(ctx, v.ID, "s", map[string]string{v.Questions[0].ID: "3"}); !errors.Is(err, errAlreadySubmitted) {
		t.Fatalf("saved after submitting: %v", err)
	}
	if n := count(t, p, "SELECT count(*) FROM saved_answers"); n != 0 {
		t.Fatalf("saved work written after submitting: %d", n)
	}
	saved, err := p.findSaved(ctx, v.ID, "s")
	if err != nil || len(saved.Answers) != 0 || saved.SavedAt != nil {
		t.Fatalf("%+v %v", saved, err)
	}
}

func TestTheOutboxEventMatchesItsContract(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, Late: true, SubmittedAt: time.Now()}
	if _, err := p.insertSubmission(ctx, v, sub); err != nil {
		t.Fatal(err)
	}
	var body []byte
	if err := p.db.QueryRow(ctx, "SELECT body FROM outbox WHERE partition_key=$1", sub.ID).Scan(&body); err != nil {
		t.Fatal(err)
	}
	var event any
	if err := json.Unmarshal(body, &event); err != nil {
		t.Fatal(err)
	}
	for _, problem := range conform("event", submittedSchema(t), event) {
		t.Error(problem)
	}
}

func TestAFailedEventLeavesNoSubmission(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	if _, err := p.saveAnswers(ctx, v.ID, "s", map[string]string{v.Questions[0].ID: "1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := p.db.Exec(ctx, "ALTER TABLE outbox ADD CONSTRAINT refuse CHECK (topic <> '"+topicSubmitted+"')"); err != nil {
		t.Fatal(err)
	}
	sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()}
	if ok, err := p.insertSubmission(ctx, v, sub); ok || err == nil {
		t.Fatalf("inserted %v, error %v", ok, err)
	}
	if count(t, p, "SELECT count(*) FROM submissions") != 0 || count(t, p, "SELECT count(*) FROM saved_answers") != 1 {
		t.Fatal("a submission without its event was kept")
	}
}
