package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"slices"
	"strings"
	"unicode/utf8"
)

const maxBankText = 200

func invalidBankQuestion(w http.ResponseWriter) {
	platform.Error(w, http.StatusUnprocessableEntity, "INVALID_BANK_QUESTION", "A bank question you chose is not in your bank.")
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
	platform.CanonicalIDs(&in.AssessmentID, &in.QuestionID)
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
