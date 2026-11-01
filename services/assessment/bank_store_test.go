package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// these tests run the question bank and resits (adr 0017) against real postgresql.

// account ids are uuids, as identity's deletion events require; the students sort in name order.
const (
	bankOwner    = "00000000-0000-4000-8000-0000000000f1"
	otherTeacher = "00000000-0000-4000-8000-0000000000f2"
	amy          = "00000000-0000-4000-8000-0000000000a1"
	ben          = "00000000-0000-4000-8000-0000000000a2"
	cat          = "00000000-0000-4000-8000-0000000000a3"
	dan          = "00000000-0000-4000-8000-0000000000a4"
	eve          = "00000000-0000-4000-8000-0000000000a5"
	zed          = "00000000-0000-4000-8000-0000000000a6"
)

// published resit of original for students, with its questions copied and linked to banks (index-aligned).
func resitOf(t *testing.T, p pgStore, original Assessment, students []string, banks ...string) Assessment {
	t.Helper()
	ctx := context.Background()
	v := original
	v.ID, v.ResitOf, v.Assigned, v.Assignees, v.CreatedBy = uuid.NewString(), original.ID, true, students, bankOwner
	v.Questions = slices.Clone(original.Questions)
	for i := range v.Questions {
		v.Questions[i].ID = uuid.NewString()
	}
	v.BankLinks, v.LinkOwner = banks, bankOwner
	if err := p.createResit(ctx, "org", v); err != nil {
		t.Fatal(err)
	}
	if err := p.publishDraft(ctx, "org", v.ID, time.Now()); err != nil {
		t.Fatal(err)
	}
	v.Status = statusPublished
	return v
}

// inTx runs apply in one committed transaction, returning rather than failing so goroutines may use it.
func inTx(p pgStore, apply func(tx pgx.Tx) error) error {
	ctx := context.Background()
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = apply(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func deleteUser(p pgStore, user, role string) error {
	data, _ := json.Marshal(platform.UserDeleted{User: user, Org: "org", Role: role})
	return inTx(p, func(tx pgx.Tx) error {
		return purgeUser(context.Background(), tx, platform.Event{Resource: user, Data: data})
	})
}

func deleteClassroom(p pgStore) error {
	data, _ := json.Marshal(platform.ClassroomDeleted{Classroom: room, Org: "org"})
	return inTx(p, func(tx pgx.Tx) error {
		return purge(context.Background(), tx, platform.Event{Resource: room, Data: data})
	})
}

func must(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func submitAs(p pgStore, v Assessment, student string) error {
	_, _, err := p.insertSubmission(context.Background(), v, Submission{ID: uuid.NewString(), Org: "org", Student: student, Answers: map[string]string{v.Questions[0].ID: "2"}, SubmittedAt: time.Now()})
	return err
}

func TestOnlyAssignedStudentsSeeAResit(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org", statusPublished, nil)
	v := resitOf(t, p, original, []string{amy})
	got, err := p.findAssessment(ctx, "org", v.ID)
	if err != nil || !got.Assigned || got.ResitOf != original.ID || !slices.Equal(got.Assignees, []string{amy}) || got.Status != statusPublished {
		t.Fatalf("%+v %v", got, err)
	}
	if !got.visibleTo(amy, time.Now()) || got.visibleTo(ben, time.Now()) {
		t.Fatal("visibility does not follow the list")
	}
	batch, err := p.findAssessments(ctx, "org", []string{original.ID, v.ID})
	if err != nil || len(batch) != 2 {
		t.Fatalf("%+v %v", batch, err)
	}
	for _, item := range batch {
		if item.ID == original.ID && (item.Assigned || item.Assignees != nil) || item.ID == v.ID && !slices.Equal(item.Assignees, []string{amy}) {
			t.Fatalf("the batch loaded the wrong lists: %+v", item)
		}
	}
	for student, want := range map[string]int{amy: 2, ben: 1} {
		list, err := p.listAssessments(ctx, "org", room, student, false, nil, time.Now())
		total, err2 := p.countAssessments(ctx, "org", room, student, false, time.Now())
		if err != nil || err2 != nil || len(list) != want || total != want {
			t.Errorf("%s lists %d and counts %d, want %d: %v %v", student, len(list), total, want, err, err2)
		}
	}
	if teacherTotal, err := p.countAssessments(ctx, "org", room, "", true, time.Now()); err != nil || teacherTotal != 2 {
		t.Errorf("the bankOwner counts %d: %v", teacherTotal, err)
	}
	if err = submitAs(p, v, ben); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("an unassigned student submitted: %v", err)
	}
	if _, err = p.saveAnswers(ctx, v.ID, ben, map[string]string{}); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("an unassigned student saved work: %v", err)
	}
	if err = submitAs(p, v, amy); err != nil {
		t.Errorf("the assigned student could not submit: %v", err)
	}
}

func TestAnEmptiedListHidesTheResitFromEveryone(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := resitOf(t, p, stored(t, p, "org", statusPublished, nil), []string{amy})
	must(t, deleteUser(p, amy, "student"))
	got, err := p.findAssessment(ctx, "org", v.ID)
	if err != nil || len(got.Assignees) != 0 || got.visibleTo(ben, time.Now()) {
		t.Fatalf("%+v %v", got, err)
	}
	if list, err := p.listAssessments(ctx, "org", room, ben, false, nil, time.Now()); err != nil || slices.ContainsFunc(list, func(s Summary) bool { return s.ID == v.ID }) {
		t.Fatalf("an emptied resit is listed: %+v %v", list, err)
	}
	if err = submitAs(p, v, ben); !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("anyone could submit an emptied resit: %v", err)
	}
}

func TestChangingAResitsList(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	whole := stored(t, p, "org", statusPublished, nil)
	v := resitOf(t, p, whole, []string{amy, ben, cat})
	if err := p.setAssignees(ctx, "org", whole.ID, []string{amy}, []string{amy}); !errors.Is(err, errNotAssigned) {
		t.Errorf("a whole-class assessment got a list: %v", err)
	}
	if err := submitAs(p, v, amy); err != nil {
		t.Fatal(err)
	}
	if _, err := p.saveAnswers(ctx, v.ID, cat, map[string]string{v.Questions[0].ID: "1"}); err != nil {
		t.Fatal(err)
	}
	if err := p.setAssignees(ctx, "org", v.ID, []string{ben}, nil); !errors.Is(err, errHasSubmissions) {
		t.Errorf("a student who submitted was removed: %v", err)
	}
	if err := p.setAssignees(ctx, "org", v.ID, []string{amy, ben, dan}, nil); !errors.Is(err, errAssessmentChanged) {
		t.Errorf("an unchecked student was added: %v", err)
	}
	// amy left the class after submitting: kept without a roster check, and cat's saved work goes with her row.
	if err := p.setAssignees(ctx, "org", v.ID, []string{amy, ben, dan}, []string{dan}); err != nil {
		t.Fatal(err)
	}
	got, _ := p.findAssessment(ctx, "org", v.ID)
	if !slices.Equal(got.Assignees, []string{amy, ben, dan}) {
		t.Fatalf("the list is %v", got.Assignees)
	}
	if n := count(t, p, "SELECT count(*) FROM saved_answers WHERE student_id=$1", cat); n != 0 {
		t.Fatal("a removed student's saved work stayed")
	}
	must(t, deleteUser(p, eve, "student"))
	var deleted errStudentDeleted
	if err := p.setAssignees(ctx, "org", v.ID, []string{amy, eve}, []string{eve}); !errors.As(err, &deleted) || deleted.student != eve {
		t.Errorf("a deleted student was added: %v", err)
	}
}

// a removal that commits while a submission or a save waits on the assessment's row is seen by both.
func TestARemovalIsSeenByAWaitingSubmissionAndSave(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := resitOf(t, p, stored(t, p, "org", statusPublished, nil), []string{amy, ben})
	tx, err := p.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT 1 FROM assessments WHERE id=$1 FOR UPDATE", v.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, "DELETE FROM assessment_assignees WHERE assessment_id=$1", v.ID); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	var submitErr, saveErr error
	wg.Go(func() { submitErr = submitAs(p, v, amy) })
	wg.Go(func() { _, saveErr = p.saveAnswers(ctx, v.ID, ben, map[string]string{}) })
	time.Sleep(300 * time.Millisecond) // both are waiting on the row lock
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if !errors.Is(submitErr, platform.ErrNotFound) || !errors.Is(saveErr, platform.ErrNotFound) {
		t.Fatalf("removed students got through: submit %v, save %v", submitErr, saveErr)
	}
}

// a kept student whose purge commits while the change waits for their account lock is refused, not put back.
func TestAPurgedStudentIsNeverPutBack(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := resitOf(t, p, stored(t, p, "org", statusPublished, nil), []string{amy, zed})
	data, _ := json.Marshal(platform.UserDeleted{User: zed, Org: "org", Role: "student"})
	tx, err := p.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err = purgeUser(ctx, tx, platform.Event{Resource: zed, Data: data}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	var changeErr error
	wg.Go(func() { changeErr = p.setAssignees(ctx, "org", v.ID, []string{amy, zed, ben}, []string{ben}) })
	time.Sleep(300 * time.Millisecond)
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	var deleted errStudentDeleted
	if !errors.As(changeErr, &deleted) {
		t.Fatalf("the change went ahead: %v", changeErr)
	}
	if n := count(t, p, "SELECT count(*) FROM assessment_assignees WHERE student_id=$1", zed); n != 0 {
		t.Fatal("the purged student is back on the list")
	}
}

// removing a student and that student's purge delete in the same order, so neither deadlocks.
func TestARemovalAndThatStudentsPurgeBothFinish(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	for range 5 {
		leaving := uuid.NewString()
		v := resitOf(t, p, stored(t, p, "org", statusPublished, nil), []string{amy, leaving})
		if _, err := p.saveAnswers(ctx, v.ID, leaving, map[string]string{}); err != nil {
			t.Fatal(err)
		}
		var wg sync.WaitGroup
		var changeErr, purgeErr error
		wg.Go(func() { changeErr = p.setAssignees(ctx, "org", v.ID, []string{amy}, nil) })
		wg.Go(func() { purgeErr = deleteUser(p, leaving, "student") })
		wg.Wait()
		if changeErr != nil || purgeErr != nil {
			t.Fatal(changeErr, purgeErr)
		}
		if n := count(t, p, "SELECT count(*) FROM assessment_assignees WHERE student_id=$1", leaving); n != 0 {
			t.Fatal("the purged student is still assigned")
		}
	}
}

func TestAResitNeverOutlivesItsClassroom(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org", statusPublished, nil)
	v := original
	v.ID, v.ResitOf, v.Assigned, v.Assignees, v.LinkOwner = uuid.NewString(), original.ID, true, []string{amy}, bankOwner
	data, _ := json.Marshal(platform.ClassroomDeleted{Classroom: room, Org: "org"})
	tx, err := p.db.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err = purge(ctx, tx, platform.Event{Resource: room, Data: data}); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	var createErr error
	wg.Go(func() { createErr = p.createResit(ctx, "org", v) })
	time.Sleep(300 * time.Millisecond)
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if !errors.Is(createErr, errClassroomPurged) {
		t.Fatalf("a resit was created during the purge: %v", createErr)
	}
	if n := count(t, p, "SELECT count(*) FROM assessments WHERE id=$1", v.ID); n != 0 {
		t.Fatal("an orphan resit was stored")
	}
}

func TestAResitRefusesDeletedStudentsAndOthersBankQuestions(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org", statusPublished, nil)
	theirs, err := p.insertBankQuestion(ctx, "org", otherTeacher, original.Questions[0], nil)
	if err != nil {
		t.Fatal(err)
	}
	v := original
	v.ID, v.ResitOf, v.Assigned, v.Assignees, v.LinkOwner = uuid.NewString(), original.ID, true, []string{amy}, bankOwner
	v.BankLinks = []string{theirs}
	if err = p.createResit(ctx, "org", v); !errors.Is(err, errInvalidBankQuestion) {
		t.Errorf("a resit used another bankOwner's bank question: %v", err)
	}
	must(t, deleteUser(p, amy, "student"))
	v.BankLinks = nil
	var deleted errStudentDeleted
	if err = p.createResit(ctx, "org", v); !errors.As(err, &deleted) {
		t.Errorf("a resit was assigned to a deleted student: %v", err)
	}
	if n := count(t, p, "SELECT count(*) FROM assessments WHERE id=$1", v.ID); n != 0 {
		t.Fatal("a refused resit was stored")
	}
}

func TestSavingToTheBank(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusPublished, nil)
	q := v.Questions[0]
	q.Sources = []Excerpt{{Text: "from the slides"}}
	q.MarkingScheme = []Excerpt{{Text: "award a mark"}}
	from := &bankSource{v.ID, q.ID}
	id, err := p.insertBankQuestion(ctx, "org", bankOwner, q, from)
	if err != nil {
		t.Fatal(err)
	}
	got, err := p.findBankQuestion(ctx, "org", bankOwner, id)
	if err != nil || got.ID != id || got.Prompt != q.Prompt || count(t, p, "SELECT count(*) FROM bank_questions WHERE question ? 'sources' OR question ? 'marking_scheme' OR question->>'id'<>''") != 0 {
		t.Fatalf("the bank kept passages or an id: %+v %v", got, err)
	}
	if _, err = p.findBankQuestion(ctx, "org", otherTeacher, id); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("another bankOwner read the bank question: %v", err)
	}
	var already errAlreadyInBank
	if _, err = p.insertBankQuestion(ctx, "org", bankOwner, q, from); !errors.As(err, &already) || already.id != id {
		t.Errorf("a linked question was saved twice: %v", err)
	}
	if links, err := p.findLinks(ctx, "org", v.ID, bankOwner); err != nil || links[q.ID] != id {
		t.Fatalf("%v %v", links, err)
	}
	// after the bank question is deleted, saving again replaces the link.
	if err = p.deleteBankQuestion(ctx, "org", bankOwner, id); err != nil {
		t.Fatal(err)
	}
	if links, _ := p.findLinks(ctx, "org", v.ID, bankOwner); len(links) != 0 {
		t.Fatalf("a link to a deleted bank question was read: %v", links)
	}
	again, err := p.insertBankQuestion(ctx, "org", bankOwner, q, from)
	if err != nil {
		t.Fatal(err)
	}
	if links, _ := p.findLinks(ctx, "org", v.ID, bankOwner); links[q.ID] != again {
		t.Fatalf("the link was not replaced: %v", links)
	}
	if _, err = p.insertBankQuestion(ctx, "org", bankOwner, q, &bankSource{v.ID, uuid.NewString()}); !errors.Is(err, errAssessmentChanged) {
		t.Errorf("a vanished question was saved: %v", err)
	}
	if err = p.updateBankQuestion(ctx, "org", otherTeacher, again, q); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("another bankOwner changed the bank question: %v", err)
	}
	q.Prompt = "2 + 2?"
	if err = p.updateBankQuestion(ctx, "org", bankOwner, again, q); err != nil {
		t.Fatal(err)
	}
	if got, _ = p.findBankQuestion(ctx, "org", bankOwner, again); got.Prompt != "2 + 2?" {
		t.Fatalf("%+v", got)
	}
}

// two saves into a bank one short of full cannot both pass the count.
func TestTheBankLimitHoldsUnderConcurrentSaves(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	q := fixture().Questions[0]
	if _, err := p.db.Exec(ctx, `INSERT INTO bank_questions(id,org_id,owner_id,question,topic_id,type)
		SELECT gen_random_uuid(),'org',$1,'{}',$2,'numerical' FROM generate_series(1,$3)`, bankOwner, q.Topic, maxBank-1); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i := range errs {
		wg.Go(func() { _, errs[i] = p.insertBankQuestion(ctx, "org", bankOwner, q, nil) })
	}
	wg.Wait()
	full := 0
	for _, err := range errs {
		switch {
		case errors.Is(err, errBankFull):
			full++
		case err != nil:
			t.Fatal(err)
		}
	}
	if full != 1 || count(t, p, "SELECT count(*) FROM bank_questions WHERE owner_id=$1", bankOwner) != maxBank {
		t.Fatalf("%d saves were refused", full)
	}
}

func TestTheBankListFiltersAndKnowsWhatAStudentAnswered(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org", statusPublished, nil)
	answered, err := p.insertBankQuestion(ctx, "org", bankOwner, original.Questions[0], &bankSource{original.ID, original.Questions[0].ID})
	if err != nil {
		t.Fatal(err)
	}
	fresh := essay()
	fresh.Origin = originGenerated
	fresh.Rubric.Criteria[0].Levels = []Level{{0, "Missing"}, {2, "Explains it"}}
	untouched, err := p.insertBankQuestion(ctx, "org", bankOwner, fresh, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.insertBankQuestion(ctx, "org", otherTeacher, fresh, nil); err != nil {
		t.Fatal(err)
	}
	if err = submitAs(p, original, amy); err != nil {
		t.Fatal(err)
	}
	// a resit of the original carries the link along, and a resit of that resit too.
	first := resitOf(t, p, original, []string{ben}, answered)
	second := resitOf(t, p, first, []string{cat}, answered)
	if err = submitAs(p, second, cat); err != nil {
		t.Fatal(err)
	}
	all, total, err := p.listBank(ctx, "org", bankOwner, bankFilter{}, platform.ListWindow{Limit: 10})
	if err != nil || total != 2 || len(all) != 2 || all[0].ID != untouched || all[0].Answered != nil {
		t.Fatalf("%+v %d %v", all, total, err)
	}
	if theirs, total, err := p.listBank(ctx, "org", otherTeacher, bankFilter{}, platform.ListWindow{Limit: 10}); err != nil || total != 1 || theirs[0].ID == untouched {
		t.Fatalf("another teacher's list: %+v %v", theirs, err)
	}
	for name, c := range map[string]struct {
		f    bankFilter
		want []string
	}{
		"type":              {bankFilter{kind: typeFreeResponse}, []string{untouched}},
		"origin":            {bankFilter{origin: originGenerated}, []string{untouched}},
		"text":              {bankFilter{text: "COUNTER"}, []string{untouched}},
		"topic":             {bankFilter{topics: []string{fresh.Topic}}, []string{untouched, answered}},
		"other topic":       {bankFilter{topics: []string{uuid.NewString()}}, nil},
		"answered by amy":   {bankFilter{student: amy, excludeAnswered: true}, []string{untouched}},
		"answered by cat":   {bankFilter{student: cat, excludeAnswered: true}, []string{untouched}},
		"not answered, ben": {bankFilter{student: ben, excludeAnswered: true}, []string{untouched, answered}},
	} {
		items, total, err := p.listBank(ctx, "org", bankOwner, c.f, platform.ListWindow{Limit: 10})
		var ids []string
		for _, item := range items {
			ids = append(ids, item.ID)
		}
		if err != nil || total != len(c.want) || !slices.Equal(ids, c.want) {
			t.Errorf("%s: %v (total %d), want %v: %v", name, ids, total, c.want, err)
		}
	}
	marked, _, err := p.listBank(ctx, "org", bankOwner, bankFilter{student: amy}, platform.ListWindow{Limit: 10})
	if err != nil || len(marked) != 2 || marked[0].Answered == nil || *marked[0].Answered || !*marked[1].Answered {
		t.Fatalf("%+v %v", marked, err)
	}
	page, total, err := p.listBank(ctx, "org", bankOwner, bankFilter{}, platform.ListWindow{Offset: 1, Limit: 1})
	if err != nil || total != 2 || len(page) != 1 || page[0].ID != answered {
		t.Fatalf("%+v %d %v", page, total, err)
	}
}

// a draft's links are written again on every save, and only for the bankOwner's own bank questions.
func TestADraftSaveRewritesItsLinks(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	v := stored(t, p, "org", statusDraft, nil)
	mine, err := p.insertBankQuestion(ctx, "org", bankOwner, v.Questions[0], nil)
	if err != nil {
		t.Fatal(err)
	}
	theirs, err := p.insertBankQuestion(ctx, "org", otherTeacher, v.Questions[0], nil)
	if err != nil {
		t.Fatal(err)
	}
	changed := v
	changed.Questions = []Question{v.Questions[0], v.Questions[0]}
	changed.Questions[0].ID, changed.Questions[1].ID = uuid.NewString(), uuid.NewString()
	changed.BankLinks, changed.LinkOwner = []string{mine, theirs}, bankOwner
	if err = p.updateAssessment(ctx, "org", v, changed); err != nil {
		t.Fatal(err)
	}
	links, err := p.findLinks(ctx, "org", v.ID, bankOwner)
	if err != nil || len(links) != 1 || links[changed.Questions[0].ID] != mine {
		t.Fatalf("%v %v", links, err)
	}
	if n := count(t, p, "SELECT count(*) FROM question_bank_links WHERE assessment_id=$1", v.ID); n != 1 {
		t.Fatalf("%d links, another bankOwner's was kept", n)
	}
	again := changed
	again.Questions = slices.Clone(changed.Questions)
	again.Questions[0].ID = uuid.NewString()
	again.BankLinks = nil
	if err = p.updateAssessment(ctx, "org", changed, again); err != nil {
		t.Fatal(err)
	}
	if n := count(t, p, "SELECT count(*) FROM question_bank_links WHERE assessment_id=$1", v.ID); n != 0 {
		t.Fatal("a save without links kept the old ones")
	}
}

// deleting a bankOwner's or a student's account, or the classroom, twice over, removes what each owns and no
// more; the bank survives its classroom.
func TestPurgesReachTheBankAndTheLists(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org", statusPublished, nil)
	bank, err := p.insertBankQuestion(ctx, "org", bankOwner, original.Questions[0], &bankSource{original.ID, original.Questions[0].ID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.insertBankQuestion(ctx, "org", otherTeacher, original.Questions[0], nil); err != nil {
		t.Fatal(err)
	}
	v := resitOf(t, p, original, []string{amy, ben}, bank)
	for range 2 {
		must(t, deleteUser(p, amy, "student"))
	}
	if got, _ := p.findAssessment(ctx, "org", v.ID); !slices.Equal(got.Assignees, []string{ben}) {
		t.Fatalf("the list is %v", got.Assignees)
	}
	for range 2 {
		must(t, deleteClassroom(p))
	}
	for what, sql := range map[string]string{
		"assessments": "SELECT count(*) FROM assessments",
		"links":       "SELECT count(*) FROM question_bank_links",
		"assignees":   "SELECT count(*) FROM assessment_assignees",
	} {
		if n := count(t, p, sql); n != 0 {
			t.Errorf("%d %s outlived the classroom", n, what)
		}
	}
	if n := count(t, p, "SELECT count(*) FROM bank_questions WHERE owner_id=$1", bankOwner); n != 1 {
		t.Fatal("the bank did not survive its classroom")
	}
	for range 2 {
		must(t, deleteUser(p, bankOwner, "teacher"))
	}
	if n := count(t, p, "SELECT count(*) FROM bank_questions WHERE owner_id=$1", bankOwner); n != 0 {
		t.Fatal("a deleted bankOwner's bank stayed")
	}
	if n := count(t, p, "SELECT count(*) FROM bank_questions WHERE owner_id=$1", otherTeacher); n != 1 {
		t.Fatal("another bankOwner lost their bank")
	}
	if _, err = p.insertBankQuestion(ctx, "org", bankOwner, original.Questions[0], nil); !errors.Is(err, errAccountDeleted) {
		t.Fatalf("a deleted bankOwner saved to the bank: %v", err)
	}
}

// with no foreign key between links and the bank, a bank delete never waits on a classroom purge, a bankOwner
// purge or a draft save.
func TestABankDeleteRacesPurgesAndSavesWithoutDeadlock(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	for range 5 {
		a := stored(t, p, "org", statusDraft, nil)
		b := stored(t, p, "org", statusPublished, nil)
		bank, err := p.insertBankQuestion(ctx, "org", otherTeacher, a.Questions[0], nil)
		if err != nil {
			t.Fatal(err)
		}
		for _, v := range []Assessment{a, b} {
			if _, err = p.db.Exec(ctx, "INSERT INTO question_bank_links VALUES($1,$2,$3)", v.ID, v.Questions[0].ID, bank); err != nil {
				t.Fatal(err)
			}
		}
		changed := a
		changed.Questions = slices.Clone(a.Questions)
		changed.Questions[0].ID = uuid.NewString()
		changed.BankLinks, changed.LinkOwner = []string{bank}, otherTeacher
		var wg sync.WaitGroup
		errs := make([]error, 3)
		wg.Go(func() { errs[0] = p.deleteBankQuestion(ctx, "org", otherTeacher, bank) })
		wg.Go(func() { errs[1] = p.updateAssessment(ctx, "org", a, changed) })
		wg.Go(func() { errs[2] = deleteClassroom(p) })
		wg.Wait()
		for _, err := range errs {
			if err != nil && !errors.Is(err, platform.ErrNotFound) {
				t.Fatal(err)
			}
		}
		if _, err = p.db.Exec(ctx, "DELETE FROM purged_classrooms"); err != nil {
			t.Fatal(err)
		}
	}
}

// one teacher's bank and resits in one organization are invisible from another.
func TestTheBankAndResitsKeepOrganizationsApart(t *testing.T) {
	p, ctx := testStore(t), context.Background()
	original := stored(t, p, "org-b", statusPublished, nil)
	id, err := p.insertBankQuestion(ctx, "org-b", bankOwner, original.Questions[0], &bankSource{original.ID, original.Questions[0].ID})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = p.insertBankQuestion(ctx, "org", bankOwner, original.Questions[0], &bankSource{original.ID, original.Questions[0].ID}); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("saved from another organization's assessment: %v", err)
	}
	if items, total, err := p.listBank(ctx, "org", bankOwner, bankFilter{}, platform.ListWindow{Limit: 10}); err != nil || total != 0 || len(items) != 0 {
		t.Errorf("another organization's bank was listed: %+v %v", items, err)
	}
	if links, err := p.findLinks(ctx, "org", original.ID, bankOwner); err != nil || len(links) != 0 {
		t.Errorf("another organization's links were read: %v %v", links, err)
	}
	v := original
	v.ID, v.ResitOf, v.Assigned, v.Assignees, v.BankLinks, v.LinkOwner = uuid.NewString(), original.ID, true, []string{amy}, []string{id}, bankOwner
	if err = p.createResit(ctx, "org", v); !errors.Is(err, errInvalidBankQuestion) {
		t.Errorf("a resit used another organization's bank question: %v", err)
	}
	v.ID = uuid.NewString()
	if err = p.createResit(ctx, "org-b", v); err != nil {
		t.Fatal(err)
	}
	if links, err := p.findLinks(ctx, "org-b", v.ID, bankOwner); err != nil || links[v.Questions[0].ID] != id {
		t.Fatalf("%v %v", links, err)
	}
	if links, err := p.findLinks(ctx, "org-b", v.ID, otherTeacher); err != nil || len(links) != 0 {
		t.Errorf("another teacher read the links: %v %v", links, err)
	}
	if items, total, err := p.listBank(ctx, "org-b", bankOwner, bankFilter{}, platform.ListWindow{Limit: 10}); err != nil || total != 1 || items[0].ID != id {
		t.Errorf("%+v %v", items, err)
	}
	if err = p.setAssignees(ctx, "org", v.ID, []string{amy}, nil); !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("changed another organization's resit: %v", err)
	}
	if err = p.setAssignees(ctx, "org-b", v.ID, []string{amy, ben}, []string{ben}); err != nil {
		t.Fatal(err)
	}
}
