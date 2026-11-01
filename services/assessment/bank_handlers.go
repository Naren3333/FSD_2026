package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"slices"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
)

// resit and bank request limits (adr 0017): a resit for more students is better given as a new assessment
// for the class.
const (
	maxAssignees = 100
	maxBankText  = 200
)

// resit makes a draft resit of a published assessment for chosen students, from its questions, the
// teacher's bank questions, or both. curriculum and identity are asked before the store takes any lock.
func (s server) resit(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	original, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, original.Classroom) {
		return
	}
	var in struct {
		StudentIDs      []string `json:"student_ids"`
		Title           string   `json:"title"`
		CopyQuestionIDs []string `json:"copy_question_ids"`
		BankQuestionIDs []string `json:"bank_question_ids"`
	}
	if !platform.Decode(w, r, &in) {
		return
	}
	if !resitable(w, original, in.CopyQuestionIDs, in.BankQuestionIDs) {
		return
	}
	if _, ok = s.assignable(w, r, original.Classroom, in.StudentIDs, nil); !ok {
		return
	}
	actor := platform.Actor(r)
	v := Assessment{Classroom: original.Classroom, Title: resitTitle(in.Title, original.Title), Status: statusDraft,
		LatePolicy: original.LatePolicy, ReleasePolicy: original.ReleasePolicy, AIMarking: original.AIMarking,
		CreatedBy: actor.Subject, LinkOwner: actor.Subject, Assigned: true, ResitOf: original.ID, Assignees: in.StudentIDs}
	if !s.resitQuestions(w, r, original, &v, in.CopyQuestionIDs, in.BankQuestionIDs) {
		return
	}
	v.AIMarking = v.AIMarking && v.teacherMarked()
	if !s.prepare(w, r, &v, nil) {
		return
	}
	v.ID = uuid.NewString()
	switch err := s.store.createResit(r.Context(), actor.Org, v); {
	case errors.Is(err, errClassroomPurged):
		platform.NotFound(w, "Classroom not found.")
	case errors.Is(err, errInvalidBankQuestion):
		invalidBankQuestion(w)
	case !s.assigneeResult(w, err):
	default:
		s.teacherJSON(w, r, http.StatusCreated, v)
	}
}

// resitable checks that original is published and the resit asks for 1–50 different questions.
func resitable(w http.ResponseWriter, original Assessment, copies, banks []string) bool {
	if original.Status != statusPublished {
		platform.Error(w, http.StatusConflict, "NOT_PUBLISHED", "Only a published assessment can be resat.")
		return false
	}
	n := len(copies) + len(banks)
	if n < 1 || n > maxQuestions || !distinct(copies) || !distinct(banks) {
		platform.Invalid(w, "Choose 1–50 different questions for the resit.")
		return false
	}
	return true
}

// resitQuestions gives v the original's questions named by copies, with their links, then the bank
// questions named by banks, linked to them; it writes the error when one is missing.
func (s server) resitQuestions(w http.ResponseWriter, r *http.Request, original Assessment, v *Assessment, copies, banks []string) bool {
	actor := platform.Actor(r)
	links, err := s.store.findLinks(r.Context(), actor.Org, original.ID, actor.Subject)
	if err != nil {
		platform.Fail(w, err)
		return false
	}
	for _, id := range copies {
		i := slices.IndexFunc(original.Questions, func(q Question) bool { return q.ID == id })
		if i < 0 {
			platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "A question you chose is no longer on the assessment. Open it again and choose from it as it is now.")
			return false
		}
		v.Questions, v.BankLinks = append(v.Questions, original.Questions[i]), append(v.BankLinks, links[id])
	}
	banked, err := s.store.findBankQuestions(r.Context(), actor.Org, actor.Subject, banks)
	if err != nil {
		platform.Fail(w, err)
		return false
	}
	if len(banked) != len(banks) {
		invalidBankQuestion(w)
		return false
	}
	for _, id := range banks {
		i := slices.IndexFunc(banked, func(b BankQuestion) bool { return b.ID == id })
		v.Questions, v.BankLinks = append(v.Questions, banked[i].question()), append(v.BankLinks, id)
	}
	return true
}

// assignees replaces a resit's list of students; those already on it stay without a roster check, so a
// student who submitted and then left the class never blocks the list.
func (s server) assignees(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	v, ok := s.findAssessment(w, r)
	if !ok || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	var in struct {
		StudentIDs []string `json:"student_ids"`
	}
	if !platform.Decode(w, r, &in) {
		return
	}
	if !v.Assigned {
		notAssigned(w)
		return
	}
	checked, ok := s.assignable(w, r, v.Classroom, in.StudentIDs, v.Assignees)
	if !ok {
		return
	}
	err := s.store.setAssignees(r.Context(), platform.Actor(r).Org, v.ID, in.StudentIDs, checked)
	switch {
	case errors.Is(err, errNotAssigned):
		notAssigned(w)
	case errors.Is(err, errHasSubmissions):
		platform.Error(w, http.StatusConflict, "HAS_SUBMISSIONS", "A student who has submitted the resit cannot be taken off it.")
	case errors.Is(err, errAssessmentChanged):
		platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "The resit's students changed while you were saving. Open it again and choose its students as they are now.")
	case !s.assigneeResult(w, err):
	default:
		v.Assignees = slices.Sorted(slices.Values(in.StudentIDs))
		s.teacherJSON(w, r, http.StatusOK, v)
	}
}

// assignable checks a list of 1–100 different students, each on the classroom's roster unless already in
// kept, writing the error otherwise; checked are those found on the roster.
func (s server) assignable(w http.ResponseWriter, r *http.Request, classroom string, students, kept []string) (checked []string, ok bool) {
	if len(students) < 1 || len(students) > maxAssignees || !distinct(students) || slices.Contains(students, "") {
		platform.Invalid(w, "Choose 1–100 different students.")
		return nil, false
	}
	enrolled, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return nil, false
	}
	for _, student := range students {
		switch {
		case slices.Contains(enrolled, student):
			checked = append(checked, student)
		case !slices.Contains(kept, student):
			platform.Error(w, http.StatusUnprocessableEntity, "STUDENT_NOT_ENROLLED", "Every student must be enrolled in the classroom.")
			return nil, false
		}
	}
	return checked, true
}

// assigneeResult writes the response for a failed resit or list write: 422 for a deleted student, 404 when
// the assessment is gone.
func (s server) assigneeResult(w http.ResponseWriter, err error) bool {
	var deleted errStudentDeleted
	if errors.As(err, &deleted) {
		platform.Error(w, http.StatusUnprocessableEntity, "STUDENT_DELETED", "A student you chose has deleted their account.")
		return false
	}
	return platform.Found(w, err, "Assessment not found.")
}

func notAssigned(w http.ResponseWriter) {
	platform.Error(w, http.StatusConflict, "NOT_ASSIGNED", "This assessment is for the whole class; only a resit has a list of students.")
}

func invalidBankQuestion(w http.ResponseWriter) {
	platform.Error(w, http.StatusUnprocessableEntity, "INVALID_BANK_QUESTION", "A bank question you chose is not in your bank.")
}

// resitTitle is the title asked for, or the original's marked as a resit, cut to the title limit.
func resitTitle(asked, original string) string {
	title := strings.TrimSpace(asked)
	if title == "" {
		title = original + " (resit)"
	}
	if utf8.RuneCountInString(title) > maxTitle {
		title = string([]rune(title)[:maxTitle])
	}
	return title
}

func distinct(ids []string) bool {
	return len(slices.Compact(slices.Sorted(slices.Values(ids)))) == len(ids)
}

// bankList is a page of the teacher's bank, filtered; with classroom_id and student_id, a student on that
// classroom's roster, each question says whether they have answered it.
func (s server) bankList(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	f, classroom, ok := readBankFilter(w, r)
	if !ok || f.student != "" && !s.onRoster(w, r, classroom, f.student) {
		return
	}
	offset, limit, ok := platform.Page(w, r, 20, 100)
	if !ok {
		return
	}
	items, total, err := s.store.listBank(r.Context(), platform.Actor(r).Org, platform.Actor(r).Subject, f, platform.ListWindow{Offset: offset, Limit: limit})
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, platform.ListPage[BankQuestion]{Items: items, Total: total})
}

// readBankFilter reads and checks the bank list's filters, and the classroom named for the student.
func readBankFilter(w http.ResponseWriter, r *http.Request) (bankFilter, string, bool) {
	query := r.URL.Query()
	f := bankFilter{topics: query["topic_id"], kind: query.Get("type"), text: strings.TrimSpace(query.Get("q")), origin: query.Get("origin"),
		student: query.Get("student_id"), excludeAnswered: query.Get("exclude_answered") == "true"}
	_, known := questionKinds[f.kind]
	classroom := query.Get("classroom_id")
	var problem string
	switch {
	case slices.ContainsFunc(f.topics, func(topic string) bool { return !platform.ID(topic) }):
		problem = "topic_id must be a topic identifier."
	case f.kind != "" && !known:
		problem = "type must be a question type."
	case f.origin != "" && f.origin != originGenerated:
		problem = "origin can only be generated."
	case utf8.RuneCountInString(f.text) > maxBankText:
		problem = "Search with at most 200 characters."
	case (classroom == "") != (f.student == "") || f.excludeAnswered && f.student == "":
		problem = "Give classroom_id and student_id together to see what a student has answered."
	default:
		return f, classroom, true
	}
	platform.Invalid(w, problem)
	return f, classroom, false
}

// onRoster checks that the caller teaches classroom and student is on its roster, so no teacher learns
// what a student they do not teach has answered.
func (s server) onRoster(w http.ResponseWriter, r *http.Request, classroom, student string) bool {
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return false
	}
	enrolled, ok := s.roster.Roster(w, r, classroom)
	if ok && !slices.Contains(enrolled, student) {
		platform.NotFound(w, "Student not found.")
		return false
	}
	return ok
}

// bankCreate keeps a question in the teacher's bank: one written for it, or one saved from an assessment
// they teach, which is then linked to it.
func (s server) bankCreate(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var in struct {
		Question     *Question `json:"question"`
		AssessmentID string    `json:"assessment_id"`
		QuestionID   string    `json:"question_id"`
	}
	if !platform.Decode(w, r, &in) {
		return
	}
	var q Question
	var from *bankSource
	switch {
	case in.Question != nil && in.AssessmentID == "" && in.QuestionID == "":
		q = *in.Question
		if !s.bankQuestion(w, r, &q) {
			return
		}
	case in.Question == nil && platform.ID(in.AssessmentID) && in.QuestionID != "":
		var ok bool
		if q, ok = s.savedQuestion(w, r, in.AssessmentID, in.QuestionID); !ok {
			return
		}
		from = &bankSource{in.AssessmentID, in.QuestionID}
	default:
		platform.Invalid(w, "Send a question, or the assessment_id and question_id of one to save.")
		return
	}
	actor := platform.Actor(r)
	id, err := s.store.insertBankQuestion(r.Context(), actor.Org, actor.Subject, q, from)
	if !s.bankResult(w, err) {
		return
	}
	saved, err := s.store.findBankQuestion(r.Context(), actor.Org, actor.Subject, id)
	if !platform.Found(w, err, "Bank question not found.") {
		return
	}
	platform.JSON(w, http.StatusCreated, saved)
}

// savedQuestion is the question of an assessment the caller teaches, as the bank keeps it, with its topic
// checked.
func (s server) savedQuestion(w http.ResponseWriter, r *http.Request, assessment, question string) (Question, bool) {
	v, err := s.store.findAssessment(r.Context(), platform.Actor(r).Org, assessment)
	if !platform.Found(w, err, "Assessment not found.") || !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return Question{}, false
	}
	i := slices.IndexFunc(v.Questions, func(q Question) bool { return q.ID == question })
	if i < 0 {
		platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "This question changed or was removed since you opened the assessment. Open it again and save the question as it is now.")
		return Question{}, false
	}
	q := bankCopy(v.Questions[i])
	return q, s.topicsExist(w, r, []string{q.Topic})
}

func (s server) bankGet(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !bankPath(w, r) {
		return
	}
	v, err := s.store.findBankQuestion(r.Context(), platform.Actor(r).Org, platform.Actor(r).Subject, r.PathValue("id"))
	if platform.Found(w, err, "Bank question not found.") {
		platform.JSON(w, http.StatusOK, v)
	}
}

// bankUpdate replaces one of the teacher's bank questions; assessments holding copies of it keep theirs.
func (s server) bankUpdate(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !bankPath(w, r) {
		return
	}
	var q Question
	if !platform.Decode(w, r, &q) || !s.bankQuestion(w, r, &q) {
		return
	}
	actor, id := platform.Actor(r), r.PathValue("id")
	if !s.bankResult(w, s.store.updateBankQuestion(r.Context(), actor.Org, actor.Subject, id, q)) {
		return
	}
	v, err := s.store.findBankQuestion(r.Context(), actor.Org, actor.Subject, id)
	if platform.Found(w, err, "Bank question not found.") {
		platform.JSON(w, http.StatusOK, v)
	}
}

// bankDelete removes one of the teacher's bank questions; assessments holding copies of it keep theirs.
func (s server) bankDelete(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !bankPath(w, r) {
		return
	}
	if s.bankResult(w, s.store.deleteBankQuestion(r.Context(), platform.Actor(r).Org, platform.Actor(r).Subject, r.PathValue("id"))) {
		w.WriteHeader(http.StatusNoContent)
	}
}

func bankPath(w http.ResponseWriter, r *http.Request) bool {
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Bank question not found.")
		return false
	}
	return true
}

// bankQuestion fills in and checks a question the teacher wrote for the bank, as an assessment's question is
// checked, then its topic with curriculum. the bank keeps no passages or marking points.
func (s server) bankQuestion(w http.ResponseWriter, r *http.Request, q *Question) bool {
	if len(q.Sources) > 0 || len(q.MarkingScheme) > 0 {
		platform.Invalid(w, "The question bank keeps no source passages or marking points; they stay with the assessment they belong to.")
		return false
	}
	holder := Assessment{Questions: []Question{*q}}
	holder.normalize()
	*q = holder.Questions[0]
	if err := q.validate(); err != nil {
		platform.Invalid(w, err.Error())
		return false
	}
	return s.topicsExist(w, r, []string{q.Topic})
}

// bankResult writes the response for a failed bank write.
func (s server) bankResult(w http.ResponseWriter, err error) bool {
	var already errAlreadyInBank
	switch {
	case errors.Is(err, errBankFull):
		platform.Error(w, http.StatusConflict, "BANK_FULL", "Your question bank holds 2,000 questions, the most it can. Delete some to save more.")
	case errors.As(err, &already):
		platform.Error(w, http.StatusConflict, "ALREADY_IN_BANK", "This question is already in your bank. Update that bank question, or save this one as a new question.")
	case errors.Is(err, errAssessmentChanged):
		platform.Error(w, http.StatusConflict, "ASSESSMENT_CHANGED", "This question changed or was removed since you opened the assessment. Open it again and save the question as it is now.")
	case errors.Is(err, errAccountDeleted):
		platform.Forbidden(w, "This account was deleted.")
	default:
		return platform.Found(w, err, "Bank question not found.")
	}
	return false
}
