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

// a larger resit should use a whole-class assessment (adr 0017).
const maxAssignees = 100

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
