package main

import (
	"errors"
	"slices"
	"time"
	"unicode/utf8"
)

// evaluation statuses: a draft until the classroom's teacher finalizes it.
const (
	statusDraft     = "draft"
	statusFinalized = "finalized"
)

// when students see a finalized grade: at once, or when the teacher releases the assessment's results.
const (
	releaseOnFinalize = "on_finalize"
	releaseManual     = "manual"
)

// who set a question's marks.
const (
	markedAuto    = "auto"
	markedTeacher = "teacher"
)

const maxFeedback = 2000

// marking-scheme passage limits, as assessment checks them.
const (
	maxExcerpts    = 5
	maxExcerptText = 2000
)

var (
	errScoreOutOfBounds = errors.New("override score out of bounds")
	errUnknownQuestion  = errors.New("override references unknown question")
	errInvalidCriteria  = errors.New("rubric marks must give every criterion one of its levels")
	errFeedbackTooLong  = errors.New("feedback too long")
	errUnmarked         = errors.New("a question still needs marking")
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
type Rubric struct {
	Criteria    []Criterion `json:"criteria"`
	ModelAnswer string      `json:"model_answer,omitempty"`
}

// excerpt is a passage of the classroom's marking scheme the teacher attached to a question, for ai marking
// suggestions only.
type Excerpt struct {
	Document string `json:"document_id,omitempty"`
	Chunk    string `json:"chunk_id,omitempty"`
	Page     int    `json:"page,omitempty"`
	Text     string `json:"text"`
}

// question is a question as assessment published it, answer key, rubric and marking-scheme passages
// included.
type Question struct {
	ID              string    `json:"id"`
	Prompt          string    `json:"prompt"`
	Type            string    `json:"type"`
	Options         []string  `json:"options"`
	Answer          string    `json:"answer"`
	Tolerance       string    `json:"tolerance"`
	AcceptedAnswers []string  `json:"accepted_answers,omitempty"`
	CaseSensitive   bool      `json:"case_sensitive,omitempty"`
	Rubric          *Rubric   `json:"rubric,omitempty"`
	MaxLength       int       `json:"max_length,omitempty"`
	MarkingScheme   []Excerpt `json:"marking_scheme,omitempty"`
	Points          int       `json:"points"`
	Topic           string    `json:"topic_id"`
}

// criterionscore is the level a rubric criterion was given.
type CriterionScore struct {
	Criterion string `json:"criterion_id"`
	Points    int    `json:"points"`
}

// suggestion is an ai-suggested mark for a question that needs the teacher.
type Suggestion struct {
	Status     string               `json:"status"` // pending, ready or failed
	Criteria   []SuggestedCriterion `json:"criteria,omitempty"`
	Awarded    int                  `json:"awarded"`
	Feedback   string               `json:"feedback,omitempty"`
	Confidence string               `json:"confidence,omitempty"`
	Flags      []string             `json:"flags,omitempty"`
	Model      string               `json:"model,omitempty"`
	// reason says why a suggestion failed or is still waiting (for the teacher's daily ai allowance, say).
	Reason string `json:"reason,omitempty"`
	// schemepassages is how many of the question's marking-scheme passages the model was given.
	SchemePassages int `json:"scheme_passages,omitempty"`
}

// suggestedcriterion is the level suggested for one criterion, with the student's own words that justify
// it.
type SuggestedCriterion struct {
	Criterion string   `json:"criterion_id"`
	Points    int      `json:"points"`
	Quotes    []string `json:"evidence_quotes"`
	Rationale string   `json:"rationale"`
}

// suggestion statuses.
const (
	suggestionPending = "pending"
	suggestionReady   = "ready"
	suggestionFailed  = "failed"
)

// evidence is one question's marks in an evaluation.
type Evidence struct {
	Question     string           `json:"question_id"`
	Topic        string           `json:"topic_id"`
	Correct      bool             `json:"correct"`
	Awarded      int              `json:"awarded"`
	Possible     int              `json:"possible"`
	NeedsMarking bool             `json:"needs_marking,omitempty"`
	MarkedBy     string           `json:"marked_by,omitempty"`
	Criteria     []CriterionScore `json:"criteria,omitempty"`
	Feedback     string           `json:"feedback,omitempty"`
	Suggestion   *Suggestion      `json:"suggestion,omitempty"`
}

// evaluation is the teacher's view of one submission's grading, with the student's answers.
type Evaluation struct {
	ID            string            `json:"id"`
	Assessment    string            `json:"assessment_id"`
	Classroom     string            `json:"classroom_id"`
	Student       string            `json:"student_id"`
	Status        string            `json:"status"`
	Evidence      []Evidence        `json:"evidence"`
	Answers       map[string]string `json:"answers"`
	Late          bool              `json:"late"`
	SubmittedAt   *time.Time        `json:"submitted_at"`
	ReleasePolicy string            `json:"release_policy"`
	FinalizedAt   *time.Time        `json:"finalized_at"`
	ReleasedAt    *time.Time        `json:"released_at"`
}

// ownevidence is a question's result as its student sees it: marks and feedback, never rubric details or ai
// suggestions.
type OwnEvidence struct {
	Question string `json:"question_id"`
	Topic    string `json:"topic_id"`
	Correct  bool   `json:"correct"`
	Awarded  int    `json:"awarded"`
	Possible int    `json:"possible"`
	Feedback string `json:"feedback,omitempty"`
}

// ownresult is a student's own released result.
type OwnResult struct {
	ID          string            `json:"id"`
	Assessment  string            `json:"assessment_id"`
	Classroom   string            `json:"classroom_id"`
	Student     string            `json:"student_id"`
	Status      string            `json:"status"`
	Evidence    []OwnEvidence     `json:"evidence"`
	Answers     map[string]string `json:"answers"`
	Late        bool              `json:"late"`
	FinalizedAt *time.Time        `json:"finalized_at"`
	ReleasedAt  *time.Time        `json:"released_at"`
}

func (v Evaluation) own() OwnResult {
	evidence := make([]OwnEvidence, len(v.Evidence))
	for i, e := range v.Evidence {
		evidence[i] = OwnEvidence{e.Question, e.Topic, e.Correct, e.Awarded, e.Possible, e.Feedback}
	}
	return OwnResult{v.ID, v.Assessment, v.Classroom, v.Student, v.Status, evidence, v.Answers, v.Late, v.FinalizedAt, v.ReleasedAt}
}

// finalevidence is a question's finalized marks as grading.finalized.v3 carries them to analytics.
type FinalEvidence struct {
	Question string `json:"question_id"`
	Topic    string `json:"topic_id"`
	Correct  bool   `json:"correct"`
	Awarded  int    `json:"awarded"`
	Possible int    `json:"possible"`
}

func final(evidence []Evidence) []FinalEvidence {
	out := make([]FinalEvidence, len(evidence))
	for i, e := range evidence {
		out[i] = FinalEvidence{e.Question, e.Topic, e.Correct, e.Awarded, e.Possible}
	}
	return out
}

// mark is the teacher's marking of one question: a rubric level for every criterion of a free response, or
// the points awarded for any other question, and optional feedback for the student.
type Mark struct {
	Criteria map[string]int `json:"criteria,omitempty"`
	Awarded  *int           `json:"awarded,omitempty"`
	Feedback *string        `json:"feedback,omitempty"`
}

// overridesasmarks turns finalize's older overrides (points by question) into marks.
func overridesAsMarks(overrides map[string]int, marks map[string]Mark) map[string]Mark {
	out := map[string]Mark{}
	for id, m := range marks {
		out[id] = m
	}
	for id, score := range overrides {
		m := out[id]
		m.Awarded = &score
		out[id] = m
	}
	return out
}

// applymarks updates evidence in place with the teacher's marks, checking each against its question.
func applyMarks(evidence []Evidence, questions []Question, marks map[string]Mark) error {
	applied := 0
	for i := range evidence {
		m, ok := marks[evidence[i].Question]
		if !ok {
			continue
		}
		applied++
		var q *Question
		if j := slices.IndexFunc(questions, func(q Question) bool { return q.ID == evidence[i].Question }); j >= 0 {
			q = &questions[j]
		}
		if err := applyMark(&evidence[i], q, m); err != nil {
			return err
		}
	}
	if applied != len(marks) {
		return errUnknownQuestion
	}
	return nil
}

// applymark marks one question. q is nil for an evaluation stored before grading kept its questions; such a
// question can only take points.
func applyMark(e *Evidence, q *Question, m Mark) error {
	if m.Feedback != nil {
		if utf8.RuneCountInString(*m.Feedback) > maxFeedback {
			return errFeedbackTooLong
		}
		e.Feedback = *m.Feedback
	}
	rubric := q != nil && q.Rubric != nil
	switch {
	case m.Criteria != nil && m.Awarded != nil:
		return errInvalidCriteria
	case m.Criteria != nil:
		if !rubric {
			return errInvalidCriteria
		}
		scores, total, err := rubricScores(*q.Rubric, m.Criteria)
		if err != nil {
			return err
		}
		e.Criteria, e.Awarded = scores, total
	case m.Awarded != nil:
		if rubric {
			return errInvalidCriteria
		}
		if *m.Awarded < 0 || *m.Awarded > e.Possible {
			return errScoreOutOfBounds
		}
		e.Awarded = *m.Awarded
	default:
		return nil // feedback only
	}
	e.Correct = e.Awarded == e.Possible
	e.NeedsMarking = false
	e.MarkedBy = markedTeacher
	return nil
}

// rubricscores checks that given names one of each criterion's levels, and nothing else, and returns the
// scores in the rubric's order with their total.
func rubricScores(r Rubric, given map[string]int) ([]CriterionScore, int, error) {
	if len(given) != len(r.Criteria) {
		return nil, 0, errInvalidCriteria
	}
	scores := make([]CriterionScore, 0, len(r.Criteria))
	total := 0
	for _, c := range r.Criteria {
		points, ok := given[c.ID]
		if !ok || !slices.ContainsFunc(c.Levels, func(l Level) bool { return l.Points == points }) {
			return nil, 0, errInvalidCriteria
		}
		scores = append(scores, CriterionScore{c.ID, points})
		total += points
	}
	return scores, total, nil
}

// unmarked reports whether any question still waits for the teacher.
func unmarked(evidence []Evidence) bool {
	return slices.ContainsFunc(evidence, func(e Evidence) bool { return e.NeedsMarking })
}
