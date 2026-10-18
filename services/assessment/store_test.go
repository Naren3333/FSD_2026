package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
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
		"update":  p.updateAssessment(ctx, "org-b", draft, changed),
		"publish": p.publishDraft(ctx, "org-b", draft.ID, time.Now()),
		"details": p.updateDetails(ctx, "org-b", draft.ID, Details{Title: "Taken", LatePolicy: lateAccept}),
		"delete":  p.deleteAssessment(ctx, "org-b", draft.ID, true),
	} {
		if !errors.Is(err, platform.ErrNotFound) {
			t.Errorf("%s in another organization: %v", name, err)
		}
	}
	got, err := p.findAssessment(ctx, "org-a", draft.ID)
	if err != nil || got.Title != draft.Title || got.Status != statusDraft || got.CreatedBy != "teacher-1" || got.Questions[0].Points != 3 {
		t.Fatalf("%+v %v", got, err)
	}
	unpublished := published
	unpublished.Status = statusDraft
	if err = p.updateAssessment(ctx, "org-a", published, unpublished); !errors.Is(err, errNotDraft) {
		t.Fatalf("a published assessment became a draft again: %v", err)
	}
	for org, want := range map[string]int{"org-a": 2, "org-b": 0} {
		list, err := p.listAssessments(ctx, org, room, "s", true, []string{"s"}, time.Now())
		progress, err2 := p.listProgress(ctx, org, room, []string{"s"})
		if err != nil || err2 != nil || len(list) != want || len(progress) != want {
			t.Errorf("%s: %d listed, %d in progress (%v %v)", org, len(list), len(progress), err, err2)
		}
	}
}

func TestAnAssessmentChangesUntilAStudentSubmits(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusDraft, nil)
	v.Title = "Edited"
	if err := p.updateAssessment(ctx, "org", v, v); err != nil {
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
	if err = p.publishDraft(ctx, "org", v.ID, at); !errors.Is(err, errNotDraft) {
		t.Errorf("published twice: %v", err)
	}
	before := got
	got.Questions = slices.Clone(got.Questions)
	got.Questions[0].Prompt = "2 + 2?"
	if err = p.updateAssessment(ctx, "org", before, got); err != nil {
		t.Fatalf("a published assessment nobody has submitted: %v", err)
	}
	if _, err = p.saveAnswers(ctx, v.ID, "s", map[string]string{got.Questions[0].ID: "4"}); err != nil {
		t.Fatal(err)
	}
	sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{got.Questions[0].ID: "4"}, SubmittedAt: time.Now()}
	if _, err = p.insertSubmission(ctx, got, sub); err != nil {
		t.Fatal(err)
	}
	changed := got
	changed.Questions = slices.Clone(got.Questions)
	changed.Questions[0].Answer = "5"
	for name, err := range map[string]error{"update": p.updateAssessment(ctx, "org", got, changed), "delete": p.deleteAssessment(ctx, "org", v.ID, false)} {
		if !errors.Is(err, errHasSubmissions) {
			t.Errorf("%s after a submission: %v", name, err)
		}
	}
	due := time.Now().Add(time.Hour).UTC().Truncate(time.Microsecond)
	if err = p.updateDetails(ctx, "org", v.ID, Details{Title: "Renamed", DueAt: &due, LatePolicy: lateReject}); err != nil {
		t.Fatal(err)
	}
	final, err := p.findAssessment(ctx, "org", v.ID)
	if err != nil || final.Title != "Renamed" || final.DueAt == nil || !final.DueAt.Equal(due) || final.LatePolicy != lateReject || !sameMarking(final, got) {
		t.Fatalf("%+v %v", final, err)
	}
}

func TestAnUnsubmittedAssessmentIsDeletedWithItsSavedWork(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	for _, status := range []string{statusDraft, statusPublished} {
		v := stored(t, p, "org", status, nil)
		if status == statusPublished {
			if _, err := p.saveAnswers(ctx, v.ID, "s", map[string]string{v.Questions[0].ID: "1"}); err != nil {
				t.Fatal(err)
			}
		}
		if err := p.deleteAssessment(ctx, "org", v.ID, false); err != nil {
			t.Fatalf("%s: %v", status, err)
		}
		// A save that checked the assessment before the deletion finds it gone, not a broken reference.
		if _, err := p.saveAnswers(ctx, v.ID, "s", map[string]string{v.Questions[0].ID: "1"}); !errors.Is(err, platform.ErrNotFound) {
			t.Fatalf("%s: saved work on a deleted assessment: %v", status, err)
		}
	}
	if count(t, p, "SELECT count(*) FROM assessments")+count(t, p, "SELECT count(*) FROM saved_answers") != 0 {
		t.Fatal("deleting left rows behind")
	}
}

// Deleting with its submissions takes them, their unpublished events and saved work, and announces it in the same
// transaction; a draft's deletion is not announced.
func TestAnAssessmentIsDeletedWithItsSubmissions(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	for _, student := range []string{"s1", "s2"} {
		sub := Submission{ID: uuid.NewString(), Org: "org", Student: student, Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()}
		if _, err := p.insertSubmission(ctx, v, sub); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := p.saveAnswers(ctx, v.ID, "s3", map[string]string{v.Questions[0].ID: "1"}); err != nil {
		t.Fatal(err)
	}
	if _, err := p.db.Exec(ctx, "UPDATE outbox SET published_at=now() WHERE id=(SELECT id FROM outbox LIMIT 1)"); err != nil {
		t.Fatal(err)
	}
	// An event written as an older version before an upgrade holds answers too.
	if _, err := p.db.Exec(ctx, `INSERT INTO outbox(id,topic,partition_key,body) VALUES($1::uuid,'assessment.submitted.v3',$1::text,jsonb_build_object('data',jsonb_build_object('assessment_id',$2::text)))`, uuid.NewString(), v.ID); err != nil {
		t.Fatal(err)
	}
	if err := p.deleteAssessment(ctx, "org", v.ID, false); !errors.Is(err, errHasSubmissions) {
		t.Fatalf("deleted without confirming its submissions go: %v", err)
	}
	if err := p.deleteAssessment(ctx, "org", v.ID, true); err != nil {
		t.Fatal(err)
	}
	for table, sql := range map[string]string{
		"assessments":       "SELECT count(*) FROM assessments",
		"submissions":       "SELECT count(*) FROM submissions",
		"saved work":        "SELECT count(*) FROM saved_answers",
		"submission events": "SELECT count(*) FROM outbox WHERE topic LIKE 'assessment.submitted.%'",
	} {
		if n := count(t, p, sql); n != 0 {
			t.Errorf("%d %s left", n, table)
		}
	}
	var body []byte
	if err := p.db.QueryRow(ctx, "SELECT body FROM outbox WHERE topic=$1 AND partition_key=$2", topicDeleted, v.ID).Scan(&body); err != nil {
		t.Fatalf("no deletion event: %v", err)
	}
	var event map[string]any
	if err := json.Unmarshal(body, &event); err != nil {
		t.Fatal(err)
	}
	for _, problem := range conform("event", eventSchema(t, topicDeleted), event) {
		t.Error(problem)
	}
	if data, _ := event["data"].(map[string]any); data["assessment_id"] != v.ID || data["classroom_id"] != room || data["org_id"] != "org" {
		t.Fatalf("%v", event)
	}
	draft := stored(t, p, "org", statusDraft, nil)
	if err := p.deleteAssessment(ctx, "org", draft.ID, true); err != nil {
		t.Fatal(err)
	}
	if n := count(t, p, "SELECT count(*) FROM outbox WHERE topic=$1", topicDeleted); n != 1 {
		t.Fatalf("%d deletion events, want one for the published assessment only", n)
	}
}

// A submission whose answers are to questions the teacher has since changed, or to an assessment since deleted, is
// refused; one racing a teacher's change either lands first (and the change is refused) or is refused itself.
func TestASubmissionIsToTheQuestionsAsTheyAre(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	changed := v
	changed.Questions = slices.Clone(v.Questions)
	changed.Questions[0].Answer = "3"
	if err := p.updateAssessment(ctx, "org", v, changed); err != nil {
		t.Fatal(err)
	}
	// A second editor working from the version before that change is refused, so it cannot undo it unseen.
	stale := v
	stale.Title = "From another tab"
	if err := p.updateAssessment(ctx, "org", v, stale); !errors.Is(err, errAssessmentChanged) {
		t.Fatalf("a change made against an older version: %v", err)
	}
	sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()}
	if _, err := p.insertSubmission(ctx, v, sub); !errors.Is(err, errAssessmentChanged) {
		t.Fatalf("answers to the old questions: %v", err)
	}
	if err := p.deleteAssessment(ctx, "org", v.ID, false); err != nil {
		t.Fatal(err)
	}
	if _, err := p.insertSubmission(ctx, changed, sub); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("answers to a deleted assessment: %v", err)
	}

	for range 20 {
		v := stored(t, p, "org", statusPublished, nil)
		edit := v
		edit.Questions = slices.Clone(v.Questions)
		edit.Questions[0].Answer = "4"
		sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()}
		var wg sync.WaitGroup
		var submitErr, editErr error
		wg.Go(func() { _, submitErr = p.insertSubmission(ctx, v, sub) })
		wg.Go(func() { editErr = p.updateAssessment(ctx, "org", v, edit) })
		wg.Wait()
		submitted := count(t, p, "SELECT count(*) FROM submissions WHERE assessment_id=$1", v.ID) == 1
		switch {
		case submitErr == nil && submitted && errors.Is(editErr, errHasSubmissions):
		case errors.Is(submitErr, errAssessmentChanged) && !submitted && editErr == nil:
		default:
			t.Fatalf("submit %v, edit %v, submitted %v", submitErr, editErr, submitted)
		}
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
	elsewhere.ID, elsewhere.Classroom = uuid.NewString(), topic
	if err := p.insertAssessment(ctx, "org", elsewhere); err != nil {
		t.Fatal(err)
	}
	if _, err := p.insertSubmission(ctx, open, Submission{ID: uuid.NewString(), Org: "org", Student: "s", Answers: map[string]string{"q": "2"}, SubmittedAt: now}); err != nil {
		t.Fatal(err)
	}
	for all, want := range map[bool]int{false: 2, true: 4} {
		list, err := p.listAssessments(ctx, "org", room, "s", all, []string{"s"}, now)
		if err != nil || len(list) != want {
			t.Fatalf("all=%v: %+v %v", all, list, err)
		}
		if all {
			for _, v := range list {
				if v.Submissions == nil || *v.Submissions != map[bool]int{true: 1, false: 0}[v.ID == open.ID] {
					t.Errorf("the teacher's count: %+v", v)
				}
			}
			continue
		}
		for _, v := range list {
			if v.Submissions != nil {
				t.Errorf("a student saw the submission count: %+v", v)
			}
		}
		for _, v := range list {
			if v.Status != statusPublished || v.Questions != 1 || v.Points != 3 || v.Submitted != (v.ID == open.ID) || (v.ID != open.ID && v.ID != opened.ID) {
				t.Errorf("%+v", v)
			}
		}
	}
	progress, err := p.listProgress(ctx, "org", room, []string{"s"})
	if err != nil || len(progress) != 4 {
		t.Fatalf("%+v %v", progress, err)
	}
	for _, v := range progress {
		if v.Submissions != map[bool]int{true: 1, false: 0}[v.ID == open.ID] {
			t.Errorf("%+v", v)
		}
	}
	// A student since removed from the classroom keeps their submission, which the counts leave out.
	progress, err = p.listProgress(ctx, "org", room, []string{"someone else"})
	list, err2 := p.listAssessments(ctx, "org", room, "s", true, []string{}, now)
	if err != nil || err2 != nil {
		t.Fatal(err, err2)
	}
	for _, v := range progress {
		if v.Submissions != 0 {
			t.Errorf("a removed student counted: %+v", v)
		}
	}
	for _, v := range list {
		if *v.Submissions != 0 {
			t.Errorf("a removed student counted in the list: %+v", v)
		}
	}
}

// A new due date decides again which submissions are late, here and, announced once per change, in Grading; a
// submission made against the old due date takes the new one.
func TestANewDueDateDecidesLatenessAgain(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	past := time.Now().Add(-time.Hour).UTC().Truncate(time.Microsecond)
	if _, err := p.db.Exec(ctx, "UPDATE assessments SET due_at=$2 WHERE id=$1", v.ID, past); err != nil {
		t.Fatal(err)
	}
	v.DueAt = &past
	sub := Submission{ID: uuid.NewString(), Org: "org", Student: "s1", Answers: map[string]string{v.Questions[0].ID: "2"}, Late: true, SubmittedAt: time.Now()}
	if _, err := p.insertSubmission(ctx, v, sub); err != nil {
		t.Fatal(err)
	}
	lateCount := func() int {
		return count(t, p, "SELECT count(*) FROM submissions WHERE assessment_id=$1 AND late", v.ID)
	}
	if lateCount() != 1 {
		t.Fatal("the fixture's submission is not late")
	}
	due := time.Now().Add(24 * time.Hour).UTC().Truncate(time.Microsecond)
	if err := p.updateDetails(ctx, "org", v.ID, Details{Title: v.Title, DueAt: &due, LatePolicy: lateAccept}); err != nil {
		t.Fatal(err)
	}
	if lateCount() != 0 {
		t.Fatal("the extension left the submission late")
	}
	// Only a changed due date is announced: a new title is not.
	if err := p.updateDetails(ctx, "org", v.ID, Details{Title: "Renamed", DueAt: &due, LatePolicy: lateAccept}); err != nil {
		t.Fatal(err)
	}
	if err := p.updateDetails(ctx, "org", v.ID, Details{Title: "Renamed", LatePolicy: lateAccept}); err != nil {
		t.Fatal(err)
	}
	rows, err := p.db.Query(ctx, "SELECT body FROM outbox WHERE topic=$1 AND partition_key=$2 ORDER BY created_at", topicDueChanged, v.ID)
	if err != nil {
		t.Fatal(err)
	}
	bodies, err := pgx.CollectRows(rows, pgx.RowTo[[]byte])
	if err != nil || len(bodies) != 2 {
		t.Fatalf("%d due date events, %v", len(bodies), err)
	}
	for i, body := range bodies {
		var event map[string]any
		if err = json.Unmarshal(body, &event); err != nil {
			t.Fatal(err)
		}
		for _, problem := range conform("event", eventSchema(t, topicDueChanged), event) {
			t.Error(problem)
		}
		data, _ := event["data"].(map[string]any)
		if data["assessment_id"] != v.ID || data["classroom_id"] != room || (i == 1) != (data["due_at"] == nil) {
			t.Errorf("event %d: %v", i, data)
		}
	}
	// A submission checked against the old, passed due date is judged against the one stored when it commits.
	sub = Submission{ID: uuid.NewString(), Org: "org", Student: "s2", Answers: map[string]string{v.Questions[0].ID: "2"}, Late: true, SubmittedAt: time.Now()}
	if _, err = p.insertSubmission(ctx, v, sub); err != nil {
		t.Fatal(err)
	}
	if lateCount() != 0 {
		t.Fatal("a submission kept the late flag of the old due date")
	}
	draft := stored(t, p, "org", statusDraft, nil)
	if err = p.updateDetails(ctx, "org", draft.ID, Details{Title: "Draft", DueAt: &due, LatePolicy: lateAccept}); err != nil {
		t.Fatal(err)
	}
	if n := count(t, p, "SELECT count(*) FROM outbox WHERE topic=$1 AND partition_key=$2", topicDueChanged, draft.ID); n != 0 {
		t.Fatal("a draft's due date was announced")
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

func TestAPurgedClassroomRefusesNewAssessments(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	before := stored(t, p, "org", statusDraft, nil)
	data, _ := json.Marshal(platform.ClassroomDeleted{Classroom: room, Org: "org"})
	tx, err := p.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err = purge(ctx, tx, platform.Event{Resource: room, Data: data}); err != nil {
		t.Fatal(err)
	}
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if n := count(t, p, "SELECT count(*) FROM assessments WHERE id=$1", before.ID); n != 0 {
		t.Fatal("the purge left an assessment")
	}
	late := fixture()
	late.ID, late.Questions[0].ID, late.Classroom, late.Status, late.CreatedBy = uuid.NewString(), uuid.NewString(), room, statusDraft, "teacher-1"
	if err = p.insertAssessment(ctx, "org", late); !errors.Is(err, errClassroomPurged) {
		t.Fatalf("an assessment was created in a purged classroom: %v", err)
	}
	if n := count(t, p, "SELECT count(*) FROM assessments WHERE classroom_id=$1", room); n != 0 {
		t.Fatal("the refused assessment was stored")
	}
}

func TestTheListCountsQuestionsAndPointsLikeDecoding(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	id := uuid.NewString()
	if _, err := p.db.Exec(ctx, `INSERT INTO assessments(id,classroom_id,org_id,title,questions) VALUES($1,$2,'org','Old','[{"id":"q1","type":"numerical"},{"id":"q2","type":"free_response","points":7},{"id":"q3","type":"numerical","points":0}]')`, id, room); err != nil {
		t.Fatal(err)
	}
	list, err := p.listAssessments(ctx, "org", room, "student-1", true, nil, time.Now())
	if err != nil || len(list) != 1 || list[0].Questions != 3 || list[0].Points != 9 {
		t.Fatalf("%+v %v", list, err)
	}
}

// Migration 004 renames the key of questions stored before topics without changing anything else, and running it
// again changes nothing.
func TestTheTopicsMigrationRenamesStoredQuestions(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	id, topic := uuid.NewString(), uuid.NewString()
	old := `[{"id":"q1","prompt":"1 + 1?","type":"numerical","answer":"2","points":2,"skill_id":"` + topic + `"},` +
		`{"id":"q2","prompt":"Why?","type":"short_answer","accepted_answers":["x"],"skill_id":"` + topic + `","topic_id":"kept"}]`
	if _, err := p.db.Exec(ctx, "INSERT INTO assessments(id,classroom_id,org_id,title,questions) VALUES($1,$2,'org',$3,$4)", id, room, "Old", old); err != nil {
		t.Fatal(err)
	}
	sql, err := migrations.ReadFile("migrations/004_topics.sql")
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err = p.db.Exec(ctx, string(sql)); err != nil {
			t.Fatal(err)
		}
	}
	v, err := p.findAssessment(ctx, "org", id)
	if err != nil {
		t.Fatal(err)
	}
	if len(v.Questions) != 2 || v.Questions[0].ID != "q1" || v.Questions[0].Topic != topic || v.Questions[0].Points != 2 ||
		v.Questions[1].ID != "q2" || v.Questions[1].Topic != "kept" || v.Questions[1].AcceptedAnswers[0] != "x" {
		t.Fatalf("%+v", v.Questions)
	}
	if n := count(t, p, "SELECT count(*) FROM assessments, jsonb_array_elements(questions) q WHERE q ? 'skill_id'"); n != 0 {
		t.Fatalf("%d questions still have the old key", n)
	}
}

// A student who deleted their account loses their submissions, saved work and submission events, twice over
// harmlessly, and cannot submit or save again; other students and organizations keep theirs.
func TestADeletedAccountsWorkIsPurged(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	gone, kept := uuid.NewString(), uuid.NewString()
	answers := map[string]string{v.Questions[0].ID: "2"}
	for _, student := range []string{gone, kept} {
		if _, err := p.insertSubmission(ctx, v, Submission{ID: uuid.NewString(), Org: "org", Student: student, Answers: answers, SubmittedAt: time.Now()}); err != nil {
			t.Fatal(err)
		}
	}
	other := stored(t, p, "org", statusPublished, nil)
	if _, err := p.saveAnswers(ctx, other.ID, gone, map[string]string{other.Questions[0].ID: "1"}); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(platform.UserDeleted{User: gone, Org: "org", Role: "student"})
	for range 2 {
		tx, err := p.db.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if err = purgeUser(ctx, tx, platform.Event{Resource: gone, Data: data}); err != nil {
			t.Fatal(err)
		}
		if err = tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
	}
	for what, sql := range map[string]string{
		"submissions":       "SELECT count(*) FROM submissions WHERE student_id=$1",
		"saved work":        "SELECT count(*) FROM saved_answers WHERE student_id=$1",
		"submission events": "SELECT count(*) FROM outbox WHERE body->'data'->>'student_id'=$1",
	} {
		if n := count(t, p, sql, gone); n != 0 {
			t.Errorf("%d %s left", n, what)
		}
		if n := count(t, p, sql, kept); what != "saved work" && n != 1 {
			t.Errorf("another student lost %s: %d", what, n)
		}
	}
	if _, err := p.insertSubmission(ctx, other, Submission{ID: uuid.NewString(), Org: "org", Student: gone, Answers: map[string]string{other.Questions[0].ID: "2"}, SubmittedAt: time.Now()}); !errors.Is(err, errAccountDeleted) {
		t.Errorf("a deleted account submitted: %v", err)
	}
	if _, err := p.saveAnswers(ctx, other.ID, gone, map[string]string{other.Questions[0].ID: "1"}); !errors.Is(err, errAccountDeleted) {
		t.Errorf("a deleted account saved work: %v", err)
	}
	invalid, _ := json.Marshal(platform.UserDeleted{User: gone, Org: "org"})
	tx, err := p.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	var bad platform.InvalidEvent
	if err = purgeUser(ctx, tx, platform.Event{Resource: gone, Data: invalid}); !errors.As(err, &bad) {
		t.Fatalf("a deletion without a role: %v", err)
	}
}
