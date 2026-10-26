package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"slices"
	"strings"
	"unicode/utf8"
)

// server answers grading's http routes from its store, with classroom access from identity.
type server struct {
	store  store
	access platform.ClassroomAccess
	roster platform.ClassroomRoster
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/evaluations", s.list)
	mux.HandleFunc("GET /v1/evaluations/mine", s.mine)
	mux.HandleFunc("GET /v1/evaluations/mine/page", s.mine)
	mux.HandleFunc("GET /v1/evaluations/counts", s.counts)
	mux.HandleFunc("POST /v1/evaluations/release", s.release)
	mux.HandleFunc("POST /v1/evaluations/finalize", s.finalizeAll)
	mux.HandleFunc("GET /v1/evaluations/{id}", s.get)
	mux.HandleFunc("POST /v1/evaluations/{id}/marks", s.mark)
	mux.HandleFunc("POST /v1/evaluations/{id}/finalize", s.finalize)
	mux.HandleFunc("POST /v1/evaluations/{id}/reopen", s.reopen)
}

// maxPage is the most evaluations one page of a teacher's list holds; each carries its student's answers.
const maxPage = 100

// list authorized evaluations with pagination and submission ordering.
func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom, assessment, students, ok := s.enrolledWork(w, r)
	if !ok {
		return
	}
	offset, limit, ok := platform.Page(w, r, 20, maxPage)
	if !ok {
		return
	}
	result, total, err := s.store.listEvaluations(r.Context(), platform.Actor(r).Org, classroom, assessment, students, offset, limit)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"evaluations": result, "total": total})
}

// authorize the classroom teacher and load the current roster and optional assessment filter.
func (s server) enrolledWork(w http.ResponseWriter, r *http.Request) (classroom, assessment string, students []string, ok bool) {
	classroom = r.URL.Query().Get("classroom_id")
	assessment = r.URL.Query().Get("assessment_id")
	if assessment != "" && !platform.ID(assessment) {
		platform.Invalid(w, "assessment_id must be an assessment identifier.")
		return "", "", nil, false
	}
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return "", "", nil, false
	}
	students, ok = s.roster.Roster(w, r, classroom)
	if !ok {
		return "", "", nil, false
	}
	// student_id narrows the work to one enrolled student, as the performance agent reads it (adr 0014); a
	// student no longer on the roster has none.
	if student := r.URL.Query().Get("student_id"); student != "" {
		if slices.Contains(students, student) {
			students = []string{student}
		} else {
			students = []string{}
		}
	}
	return classroom, assessment, students, true
}

// get is one evaluation with its student's answers, for the classroom's teacher only, so an open grade can
// be read again (for ai suggestions still being prepared) wherever it is in the list.
func (s server) get(w http.ResponseWriter, r *http.Request) {
	if !s.teacherOfEvaluation(w, r, false) {
		return
	}
	v, err := s.store.findEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if platform.Found(w, err, "Evaluation not found.") {
		platform.JSON(w, http.StatusOK, v)
	}
}

// count draft and finalized evaluations for currently enrolled students.
func (s server) counts(w http.ResponseWriter, r *http.Request) {
	classroom, assessment, students, ok := s.enrolledWork(w, r)
	if !ok {
		return
	}
	result, err := s.store.countEvaluations(r.Context(), platform.Actor(r).Org, classroom, assessment, students)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// mine is the calling student's released results in a classroom they are enrolled in: the newest 100, or
// at /mine/page one page (offset, limit 1–100, default 20) with the total.
func (s server) mine(w http.ResponseWriter, r *http.Request) {
	c := platform.Actor(r)
	classroom := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, classroom, c.Subject)
	if !ok {
		return
	}
	if relation != platform.RelationStudent {
		platform.Forbidden(w, "Only enrolled students have results here.")
		return
	}
	var paging []platform.ListWindow
	if r.URL.Path == "/v1/evaluations/mine/page" {
		offset, limit, valid := platform.Page(w, r, 20, maxPage)
		if !valid {
			return
		}
		paging = []platform.ListWindow{{Offset: offset, Limit: limit}}
	}
	result, err := s.store.listReleased(r.Context(), c.Org, classroom, c.Subject, paging...)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	own := make([]OwnResult, len(result))
	for i, v := range result {
		own[i] = v.own()
	}
	if len(paging) == 0 {
		platform.JSON(w, http.StatusOK, own)
		return
	}
	total, err := s.store.countReleased(r.Context(), c.Org, classroom, c.Subject)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, platform.ListPage[OwnResult]{Items: own, Total: total})
}

// teacherOfEvaluation checks the caller is the teacher of the classroom of the evaluation the path names.
// a change (enrolled) also needs its student still on the roster, as lists and releases do (adr 0010).
func (s server) teacherOfEvaluation(w http.ResponseWriter, r *http.Request, enrolled bool) bool {
	// the role check comes first so a student is refused before any lookup; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return false
	}
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Evaluation not found.")
		return false
	}
	classroom, student, err := s.store.findClassroom(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Evaluation not found.") || !platform.TeacherOf(s.access, w, r, classroom) {
		return false
	}
	if !enrolled {
		return true
	}
	students, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return false
	}
	if !slices.Contains(students, student) {
		platform.Error(w, http.StatusConflict, "STUDENT_REMOVED", "This student is no longer in the classroom, so their grade cannot change. Enroll them again to grade their work.")
		return false
	}
	return true
}

// mark saves the teacher's marks on a draft without finalizing it.
func (s server) mark(w http.ResponseWriter, r *http.Request) {
	var input struct {
		Marks map[string]Mark `json:"marks"`
	}
	if !s.teacherOfEvaluation(w, r, true) || !platform.Decode(w, r, &input) {
		return
	}
	if len(input.Marks) == 0 {
		platform.Invalid(w, "Give marks for at least one question.")
		return
	}
	v, err := s.store.saveMarks(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Marks)
	if writeMarkingError(w, err) {
		platform.JSON(w, http.StatusOK, v)
	}
}

func (s server) finalize(w http.ResponseWriter, r *http.Request) {
	if !s.teacherOfEvaluation(w, r, true) {
		return
	}
	var input struct {
		Reason    string          `json:"reason"`
		Overrides map[string]int  `json:"overrides"`
		Marks     map[string]Mark `json:"marks"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !validReason(w, input.Reason) {
		return
	}
	marks := overridesAsMarks(input.Overrides, input.Marks)
	v, err := s.store.finalizeEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Reason, marks)
	if writeMarkingError(w, err) {
		platform.JSON(w, http.StatusOK, v)
	}
}

// validReason checks a review reason is 1–1000 characters, and says so when it is not.
func validReason(w http.ResponseWriter, reason string) bool {
	if strings.TrimSpace(reason) == "" || utf8.RuneCountInString(reason) > 1000 {
		platform.Invalid(w, "A review reason of 1–1000 characters is required.")
		return false
	}
	return true
}

// reopen turns a finalized grade back into a draft so its teacher can correct it (adr 0016).
func (s server) reopen(w http.ResponseWriter, r *http.Request) {
	if !s.teacherOfEvaluation(w, r, true) {
		return
	}
	var input struct {
		Reason string `json:"reason"`
	}
	if !platform.Decode(w, r, &input) || !validReason(w, input.Reason) {
		return
	}
	v, err := s.store.reopenEvaluation(r.Context(), platform.Actor(r).Org, r.PathValue("id"), platform.Actor(r).Subject, input.Reason)
	if writeMarkingError(w, err) {
		platform.JSON(w, http.StatusOK, v)
	}
}

// bulkReason is a bulk finalize's audit reason when the teacher gives none.
const bulkReason = "Finalized with the assessment's other fully marked grades"

// finalizeAll finalizes an assessment's drafts of enrolled students that need no more marking, for the
// classroom's teacher only (adr 0016).
func (s server) finalizeAll(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var input struct {
		Assessment string `json:"assessment_id"`
		Reason     string `json:"reason"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !platform.ID(input.Assessment) {
		platform.Invalid(w, "assessment_id must be an assessment identifier.")
		return
	}
	if input.Reason == "" {
		input.Reason = bulkReason
	} else if !validReason(w, input.Reason) {
		return
	}
	org := platform.Actor(r).Org
	classroom, err := s.store.assessmentClassroom(r.Context(), org, input.Assessment)
	if !platform.Found(w, err, "No grades for this assessment.") || !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	students, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return
	}
	finalized, waiting, err := s.store.finalizeAssessment(r.Context(), org, input.Assessment, platform.Actor(r).Subject, input.Reason, students)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, BulkFinalized{input.Assessment, finalized, waiting})
}

// BulkFinalized is how many of an assessment's drafts a bulk finalize finalized, and how many still need
// marking.
type BulkFinalized struct {
	Assessment   string `json:"assessment_id"`
	Finalized    int    `json:"finalized"`
	NeedsMarking int    `json:"needs_marking"`
}

// writeMarkingError writes the response for a failed marking or finalization and reports whether err was
// nil.
func writeMarkingError(w http.ResponseWriter, err error) bool {
	switch {
	case err == nil:
		return true
	case errors.Is(err, errAlreadyFinalized):
		platform.Error(w, http.StatusConflict, "ALREADY_FINALIZED", "This evaluation has already been finalized.")
	case errors.Is(err, errNotFinalized):
		platform.Error(w, http.StatusConflict, "NOT_FINALIZED", "Only a finalized grade can be reopened.")
	case errors.Is(err, errUnmarked):
		platform.Error(w, http.StatusUnprocessableEntity, "NEEDS_MARKING", "Mark every question before finalizing.")
	case errors.Is(err, errScoreOutOfBounds):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Scores must be within question bounds.")
	case errors.Is(err, errUnknownQuestion):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_OVERRIDE", "Override references an unknown question.")
	case errors.Is(err, errInvalidCriteria):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_MARK", "Give a free response one level for every rubric criterion, and other questions points.")
	case errors.Is(err, errFeedbackTooLong):
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_MARK", "Feedback is at most 2000 characters.")
	default:
		platform.Found(w, err, "Evaluation not found.")
	}
	return false
}

// release shows an assessment's finalized grades to its enrolled students, for the classroom's teacher
// only: the ones its counts report as unreleased.
func (s server) release(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var input struct {
		Assessment string `json:"assessment_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	if !platform.ID(input.Assessment) {
		platform.Invalid(w, "assessment_id must be an assessment identifier.")
		return
	}
	org := platform.Actor(r).Org
	classroom, err := s.store.assessmentClassroom(r.Context(), org, input.Assessment)
	if !platform.Found(w, err, "No grades for this assessment.") || !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	students, ok := s.roster.Roster(w, r, classroom)
	if !ok {
		return
	}
	n, err := s.store.release(r.Context(), org, input.Assessment, platform.Actor(r).Subject, students)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, map[string]any{"assessment_id": input.Assessment, "released": n})
}
