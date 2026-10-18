package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"learning/platform"
	"slices"
	"strings"
	"time"
	"unicode/utf8"
)

// Assessment limits.
const (
	maxTitle     = 160
	maxQuestions = 50
	maxPrompt    = 2000
	minOptions   = 2
	maxOptions   = 8
	maxAnswer    = 500 // also the longest option and accepted short answer
	maxPoints    = 100 // per question
)

// Assessment statuses. A draft is the teacher's to edit and invisible to students; a published assessment opens to
// the classroom's students at OpensAt (or at once). The teacher may still replace or delete a published assessment
// until a student submits it; after that only its Details change.
const (
	statusDraft     = "draft"
	statusPublished = "published"
)

// What happens to a submission after the due date, and when students see their finalized grades.
const (
	lateAccept        = "accept_flagged"
	lateReject        = "reject"
	releaseOnFinalize = "on_finalize"
	releaseManual     = "manual"
)

type Level struct {
	Points     int    `json:"points"`
	Descriptor string `json:"descriptor"`
}
type Criterion struct {
	ID     string  `json:"id"`
	Title  string  `json:"title"`
	Levels []Level `json:"levels"`
}

// Rubric is how a free response is marked. It is teacher-only, like an answer key: students never receive it.
type Rubric struct {
	Criteria    []Criterion `json:"criteria"`
	ModelAnswer string      `json:"model_answer,omitempty"`
}

// Excerpt is a passage of the classroom's marking scheme that the teacher attached to a question, for AI marking
// suggestions to read alongside the rubric. It is teacher-only, like the rubric. Picked from Content's search, it
// names its document, chunk and page; pasted by the teacher, it may name none. The ids are references the teacher
// chose, not authorization to read anything: the text itself travels with the question.
type Excerpt struct {
	Document string `json:"document_id,omitempty"`
	Chunk    string `json:"chunk_id,omitempty"`
	Page     int    `json:"page,omitempty"`
	Text     string `json:"text"`
}

type Question struct {
	ID              string   `json:"id"`
	Prompt          string   `json:"prompt"`
	Type            string   `json:"type"`
	Options         []string `json:"options"`
	Answer          string   `json:"answer"`
	Tolerance       string   `json:"tolerance"`
	AcceptedAnswers []string `json:"accepted_answers,omitempty"`
	CaseSensitive   bool     `json:"case_sensitive,omitempty"`
	Rubric          *Rubric  `json:"rubric,omitempty"`
	MaxLength       int      `json:"max_length,omitempty"`
	// MarkingScheme is for short answers and free responses only.
	MarkingScheme []Excerpt `json:"marking_scheme,omitempty"`
	Points        int       `json:"points"`
	Topic         string    `json:"topic_id"`
	// Sources are the passages of the classroom's documents a generated question came from, and Origin is "generated"
	// for a question the teacher kept from AI question generation (docs/decisions/0006-question-generation.md). Both
	// are teacher-only provenance: never in the student projection or the submission event.
	Sources []Excerpt `json:"sources,omitempty"`
	Origin  string    `json:"origin,omitempty"`
}

// originGenerated marks a question the teacher kept from AI question generation; a question without an origin was
// written by the teacher.
const originGenerated = "generated"

// Assessment is the teacher's view, with answer keys and rubrics. Status, dates and policies are optional in a
// request (normalize fills them) and always present in a response.
type Assessment struct {
	ID            string     `json:"id"`
	Classroom     string     `json:"classroom_id"`
	Title         string     `json:"title"`
	Questions     []Question `json:"questions"`
	Status        string     `json:"status"`
	OpensAt       *time.Time `json:"opens_at"`
	DueAt         *time.Time `json:"due_at"`
	LatePolicy    string     `json:"late_policy"`
	ReleasePolicy string     `json:"release_policy"`
	AIMarking     bool       `json:"ai_marking"`
	PublishedAt   *time.Time `json:"published_at"`
	// CreatedBy is the teacher who created it; their daily AI allowance pays for its AI marking (ADR 0003).
	CreatedBy string `json:"-"`
}

// Summary is an assessment in a classroom's list, with whether the caller has submitted it.
type Summary struct {
	ID         string     `json:"id"`
	Title      string     `json:"title"`
	Submitted  bool       `json:"submitted"`
	Status     string     `json:"status"`
	OpensAt    *time.Time `json:"opens_at"`
	DueAt      *time.Time `json:"due_at"`
	LatePolicy string     `json:"late_policy"`
	Questions  int        `json:"question_count"`
	Points     int        `json:"points"`
	// Submissions is how many of the enrolled students have submitted it, for the classroom's teacher only.
	Submissions *int `json:"submissions,omitempty"`
	// HasSubmissions is whether any student has submitted it, students since removed included, for the classroom's
	// teacher only: once one has, it can no longer be replaced, and deleting it deletes their work (ADR 0008).
	HasSubmissions *bool `json:"has_submissions,omitempty"`
}

// Details are what a teacher may change after students have submitted: nothing their grades depend on.
type Details struct {
	Title      string     `json:"title"`
	DueAt      *time.Time `json:"due_at"`
	LatePolicy string     `json:"late_policy"`
}

// Progress is how many students have submitted an assessment, for its classroom's teacher.
type Progress struct {
	ID          string     `json:"id"`
	Title       string     `json:"title"`
	Submissions int        `json:"submissions"`
	Status      string     `json:"status"`
	DueAt       *time.Time `json:"due_at"`
}

// Submission is one student's answers to an assessment, by question id.
type Submission struct {
	ID, Org, Student string
	Answers          map[string]string
	Late             bool
	SubmittedAt      time.Time
}

// OwnSubmission is a student's submitted answers, as they see them again: only what they wrote, never the answer keys.
type OwnSubmission struct {
	Answers     map[string]string `json:"answers"`
	SubmittedAt time.Time         `json:"submitted_at"`
	Late        bool              `json:"late"`
}

// SavedAnswers is a student's unsubmitted work on an assessment.
type SavedAnswers struct {
	Answers map[string]string `json:"answers"`
	SavedAt *time.Time        `json:"saved_at"`
}

type PublicQuestion struct {
	ID        string   `json:"id"`
	Prompt    string   `json:"prompt"`
	Type      string   `json:"type"`
	Options   []string `json:"options"`
	MaxLength int      `json:"max_length,omitempty"`
	Points    int      `json:"points"`
	Topic     string   `json:"topic_id"`
}
type PublicAssessment struct {
	ID         string           `json:"id"`
	Classroom  string           `json:"classroom_id"`
	Title      string           `json:"title"`
	Questions  []PublicQuestion `json:"questions"`
	OpensAt    *time.Time       `json:"opens_at"`
	DueAt      *time.Time       `json:"due_at"`
	LatePolicy string           `json:"late_policy"`
}

// normalize fills in what a request may leave out: published status (as before drafts existed; update keeps the
// draft's status instead), the default
// policies, one point per objective question, a free response's points from its rubric, and criterion ids.
func (v *Assessment) normalize() {
	if v.Status == "" {
		v.Status = statusPublished
	}
	if v.LatePolicy == "" {
		v.LatePolicy = lateAccept
	}
	if v.ReleasePolicy == "" {
		v.ReleasePolicy = releaseOnFinalize
	}
	for i := range v.Questions {
		q := &v.Questions[i]
		if q.Type == typeFreeResponse {
			if q.MaxLength == 0 {
				q.MaxLength = defaultMaxLength
			}
			if q.Rubric != nil {
				for j := range q.Rubric.Criteria {
					q.Rubric.Criteria[j].ID = fmt.Sprintf("c%d", j+1)
					slices.SortFunc(q.Rubric.Criteria[j].Levels, func(a, b Level) int { return a.Points - b.Points })
				}
				q.Points = q.Rubric.total()
			}
		} else if q.Points == 0 {
			q.Points = 1
		}
	}
}

func (v Assessment) Validate() error {
	if !platform.ID(v.Classroom) || strings.TrimSpace(v.Title) == "" || utf8.RuneCountInString(v.Title) > maxTitle || len(v.Questions) < 1 || len(v.Questions) > maxQuestions {
		return errors.New("provide a classroom, title, and 1–50 questions")
	}
	if err := v.validateSettings(); err != nil {
		return err
	}
	for _, q := range v.Questions {
		if err := q.validate(); err != nil {
			return err
		}
	}
	excerpts := 0
	for _, q := range v.Questions {
		excerpts += len(q.MarkingScheme)
	}
	if excerpts > maxAssessmentExcerpts {
		return fmt.Errorf("an assessment has at most %d marking-scheme passages", maxAssessmentExcerpts)
	}
	if v.AIMarking && !v.teacherMarked() {
		return errors.New("AI marking needs a short-answer or free-response question")
	}
	return nil
}

// validateSettings checks the status, policies and dates.
func (v Assessment) validateSettings() error {
	switch {
	case v.Status != statusDraft && v.Status != statusPublished:
		return errors.New("status must be draft or published")
	case v.LatePolicy != lateAccept && v.LatePolicy != lateReject:
		return errors.New("late_policy must be accept_flagged or reject")
	case v.ReleasePolicy != releaseOnFinalize && v.ReleasePolicy != releaseManual:
		return errors.New("release_policy must be on_finalize or manual")
	case v.OpensAt != nil && v.DueAt != nil && !v.DueAt.After(*v.OpensAt):
		return errors.New("the due date must be after the opening date")
	}
	return nil
}

// publishable reports why a published assessment could not be published now: a due date already past.
func (v Assessment) publishable(now time.Time) error {
	if v.DueAt != nil && !v.DueAt.After(now) {
		return errors.New("the due date has already passed")
	}
	return nil
}

func (q Question) validate() error {
	if strings.TrimSpace(q.Prompt) == "" || utf8.RuneCountInString(q.Prompt) > maxPrompt || !platform.ID(q.Topic) {
		return errors.New("each question needs a prompt and curriculum topic")
	}
	kind, ok := questionKinds[q.Type]
	if !ok {
		return errors.New("unsupported question type")
	}
	if err := kind.validate(q); err != nil {
		return err
	}
	if q.Origin != "" && q.Origin != originGenerated {
		return errors.New("a question's origin is generated or left out")
	}
	if err := validateSources(q.Sources); err != nil {
		return err
	}
	if q.Points < 1 || q.Points > maxPoints {
		if q.Type == typeFreeResponse {
			return errors.New("a free response's rubric must add up to 1–100 points (the top level of each criterion)")
		}
		return errors.New("each question is worth 1–100 points")
	}
	return nil
}

// validateDetails checks new details for v: a title, a known late policy, and a due date after the opening date and,
// when the due date changes on a published assessment, not already past.
func (v Assessment) validateDetails(d Details, now time.Time) error {
	changed := v
	changed.Title, changed.DueAt, changed.LatePolicy = d.Title, d.DueAt, d.LatePolicy
	if strings.TrimSpace(d.Title) == "" || utf8.RuneCountInString(d.Title) > maxTitle {
		return errors.New("provide a title of at most 160 characters")
	}
	if err := changed.validateSettings(); err != nil {
		return err
	}
	if v.Status == statusPublished && !sameTime(v.DueAt, d.DueAt) {
		return changed.publishable(now)
	}
	return nil
}

func sameTime(a, b *time.Time) bool {
	return (a == nil) == (b == nil) && (a == nil || a.Equal(*b))
}

// sameMarking reports whether a and b have the same questions and marking settings: whether answers to one are
// graded as answers to the other.
func sameMarking(a, b Assessment) bool {
	return a.Status == b.Status && a.ReleasePolicy == b.ReleasePolicy && a.AIMarking == b.AIMarking && sameJSON(a.Questions, b.Questions)
}

// keepQuestionIDs gives each of v's questions that is unchanged from one of before's the id it had, so the work
// students saved on it still answers it; changed and new questions keep the new ids prepare gave them.
func (v *Assessment) keepQuestionIDs(before []Question) {
	used := map[string]bool{}
	for i := range v.Questions {
		for _, old := range before {
			candidate := v.Questions[i]
			candidate.ID = old.ID
			if !used[old.ID] && sameJSON(candidate, old) {
				v.Questions[i].ID, used[old.ID] = old.ID, true
				break
			}
		}
	}
}

// sameJSON compares questions as stored, so an empty list and a missing one are alike where the JSON omits both.
func sameJSON(a, b any) bool {
	x, err1 := json.Marshal(a)
	y, err2 := json.Marshal(b)
	return err1 == nil && err2 == nil && string(x) == string(y)
}

// teacherMarked reports whether any question can need a teacher (or an AI suggestion) to mark it.
func (v Assessment) teacherMarked() bool {
	return slices.ContainsFunc(v.Questions, func(q Question) bool {
		return q.Type == typeShortAnswer || q.Type == typeFreeResponse
	})
}

// topicIDs are the distinct topics the questions assess.
func (v Assessment) topicIDs() []string {
	var ids []string
	for _, q := range v.Questions {
		if !slices.Contains(ids, q.Topic) {
			ids = append(ids, q.Topic)
		}
	}
	return ids
}

// visibleToStudents reports whether students may see and answer it: published, and open.
func (v Assessment) visibleToStudents(now time.Time) bool {
	return v.Status == statusPublished && (v.OpensAt == nil || !v.OpensAt.After(now))
}

// late reports whether a submission now is after the due date.
func (v Assessment) late(now time.Time) bool { return v.DueAt != nil && now.After(*v.DueAt) }

// closed reports whether the assessment no longer takes submissions: past its due date with late work refused.
func (v Assessment) closed(now time.Time) bool { return v.late(now) && v.LatePolicy == lateReject }

func (v Assessment) Public() PublicAssessment {
	p := PublicAssessment{v.ID, v.Classroom, v.Title, []PublicQuestion{}, v.OpensAt, v.DueAt, v.LatePolicy}
	for _, q := range v.Questions {
		p.Questions = append(p.Questions, PublicQuestion{q.ID, q.Prompt, q.Type, q.Options, q.MaxLength, q.Points, q.Topic})
	}
	return p
}

// validateAnswers reports whether answers has exactly one acceptable answer for every question.
func validateAnswers(v Assessment, answers map[string]string) bool {
	if len(answers) != len(v.Questions) {
		return false
	}
	for _, q := range v.Questions {
		answer, exists := answers[q.ID]
		kind, known := questionKinds[q.Type]
		if !exists || !known || !kind.accepts(q, answer) {
			return false
		}
	}
	return true
}

// answersCurrent reports whether every answer is to one of v's questions. An answer to any other question was made
// against a version the teacher has since changed, since a changed question gets a new id.
func answersCurrent(v Assessment, answers map[string]string) bool {
	for id := range answers {
		if !slices.ContainsFunc(v.Questions, func(q Question) bool { return q.ID == id }) {
			return false
		}
	}
	return true
}

// validateSaved reports whether saved work fits the assessment: only its questions, each within its length limit.
// Unlike a submission, saved work may leave questions out or blank.
func validateSaved(v Assessment, answers map[string]string) bool {
	for id, answer := range answers {
		i := slices.IndexFunc(v.Questions, func(q Question) bool { return q.ID == id })
		if i < 0 {
			return false
		}
		kind, known := questionKinds[v.Questions[i].Type]
		if !known || utf8.RuneCountInString(answer) > kind.limit(v.Questions[i]) {
			return false
		}
	}
	return true
}
