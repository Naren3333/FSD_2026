package main

import (
	"errors"
	"fmt"
	"learning/platform"
	"regexp"
	"slices"
	"strings"
	"unicode/utf8"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

// Question types. Multiple choice and numerical are marked by Grading alone; a short answer is marked by Grading
// when it matches an accepted answer, and otherwise by the teacher; a free response is always marked by the teacher
// against its rubric (docs/decisions/0004-assessment-lifecycle-and-ai-marking.md).
const (
	typeMultipleChoice = "multiple_choice"
	typeNumerical      = "numerical"
	typeShortAnswer    = "short_answer"
	typeFreeResponse   = "free_response"
)

// Rubric and free-response limits.
const (
	maxAccepted      = 20
	maxCriteria      = 10
	minLevels        = 2
	maxLevels        = 6
	maxCriterion     = 200
	maxDescriptor    = 500
	maxModelAnswer   = 5000
	defaultMaxLength = 5000
	maxFreeResponse  = 20000
	// Marking-scheme passages: per question, per passage and per assessment, which keeps the submission event
	// within maxSubmittedEvent.
	maxExcerpts           = 5
	maxExcerptText        = 2000
	maxExcerptPage        = 100000
	maxAssessmentExcerpts = 20
)

// questionKind holds one question type's rules: when a question of that type is well formed, and which answers a
// student may submit to it. A new type is a new kind registered in questionKinds; the callers don't change.
type questionKind interface {
	validate(q Question) error
	accepts(q Question, answer string) bool
	// limit is the longest answer, in characters, a student may give or save.
	limit(q Question) int
}

var questionKinds = map[string]questionKind{
	typeMultipleChoice: multipleChoice{},
	typeNumerical:      numerical{},
	typeShortAnswer:    shortAnswer{},
	typeFreeResponse:   freeResponse{},
}

var errForeignFields = errors.New("the question has fields its type does not use")

// objective is the shared limit and the fields an objective question must leave empty.
func objective(q Question) error {
	if len(q.AcceptedAnswers) > 0 || q.CaseSensitive || q.Rubric != nil || q.MaxLength != 0 || len(q.MarkingScheme) > 0 {
		return errForeignFields
	}
	return nil
}

// multipleChoice: 2–8 distinct options, and the answer key is one of them.
type multipleChoice struct{}

func (multipleChoice) validate(q Question) error {
	if err := objective(q); err != nil {
		return err
	}
	if len(q.Options) < minOptions || len(q.Options) > maxOptions {
		return errors.New("multiple choice needs 2–8 options")
	}
	seen := map[string]bool{}
	for _, o := range q.Options {
		if strings.TrimSpace(o) == "" || utf8.RuneCountInString(o) > maxAnswer || seen[o] {
			return errors.New("options must be unique nonempty values")
		}
		seen[o] = true
	}
	if !seen[q.Answer] {
		return errors.New("answer must match one option")
	}
	if q.Tolerance != "" {
		return errors.New("multiple choice has no tolerance")
	}
	return nil
}

func (multipleChoice) accepts(q Question, answer string) bool {
	return slices.Contains(q.Options, answer)
}

func (multipleChoice) limit(Question) int { return maxAnswer }

// numerical: a decimal answer key and a non-negative decimal tolerance, without options.
type numerical struct{}

func (numerical) validate(q Question) error {
	if err := objective(q); err != nil {
		return err
	}
	if !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) || strings.HasPrefix(q.Tolerance, "-") || len(q.Options) > 0 {
		return errors.New("numerical answers require decimal answer and nonnegative decimal tolerance, without options")
	}
	return nil
}

func (numerical) accepts(_ Question, answer string) bool { return decimal.MatchString(answer) }

func (numerical) limit(Question) int { return maxAnswer }

// shortAnswer: a list of accepted answers, matched after normalizeShort (and case-insensitively unless the
// teacher says case matters). Grading marks an unmatched answer as needing the teacher.
type shortAnswer struct{}

func (shortAnswer) validate(q Question) error {
	if len(q.Options) > 0 || q.Answer != "" || q.Tolerance != "" || q.Rubric != nil || q.MaxLength != 0 {
		return errForeignFields
	}
	if len(q.AcceptedAnswers) < 1 || len(q.AcceptedAnswers) > maxAccepted {
		return fmt.Errorf("a short answer needs 1–%d accepted answers", maxAccepted)
	}
	seen := map[string]bool{}
	for _, a := range q.AcceptedAnswers {
		key := normalizeShort(a, q.CaseSensitive)
		if key == "" || utf8.RuneCountInString(a) > maxAnswer || seen[key] {
			return errors.New("accepted answers must be unique and nonempty")
		}
		seen[key] = true
	}
	return validateScheme(q.MarkingScheme)
}

func (shortAnswer) accepts(_ Question, answer string) bool {
	return strings.TrimSpace(answer) != "" && utf8.RuneCountInString(answer) <= maxAnswer
}

func (shortAnswer) limit(Question) int { return maxAnswer }

// normalizeShort trims an answer and collapses its inner whitespace, and lowercases it unless case matters.
// Grading applies the same rule when it marks the answer.
func normalizeShort(answer string, caseSensitive bool) string {
	answer = strings.Join(strings.Fields(answer), " ")
	if !caseSensitive {
		answer = strings.ToLower(answer)
	}
	return answer
}

// freeResponse: written work marked against a rubric of criteria, each with 2–6 levels. The question's points
// are the sum of each criterion's top level (set by normalize).
type freeResponse struct{}

func (freeResponse) validate(q Question) error {
	if len(q.Options) > 0 || q.Answer != "" || q.Tolerance != "" || len(q.AcceptedAnswers) > 0 || q.CaseSensitive {
		return errForeignFields
	}
	if q.MaxLength < 1 || q.MaxLength > maxFreeResponse {
		return fmt.Errorf("a free response allows 1–%d characters", maxFreeResponse)
	}
	if q.Rubric == nil || len(q.Rubric.Criteria) < 1 || len(q.Rubric.Criteria) > maxCriteria {
		return fmt.Errorf("a free response needs a rubric of 1–%d criteria", maxCriteria)
	}
	if utf8.RuneCountInString(q.Rubric.ModelAnswer) > maxModelAnswer {
		return fmt.Errorf("a model answer has at most %d characters", maxModelAnswer)
	}
	for _, c := range q.Rubric.Criteria {
		if err := c.validate(); err != nil {
			return err
		}
	}
	return validateScheme(q.MarkingScheme)
}

// validateScheme checks a question's marking-scheme passages: up to maxExcerpts, each with text, and a chunk only
// with the document it came from.
func validateScheme(excerpts []Excerpt) error {
	if len(excerpts) > maxExcerpts {
		return fmt.Errorf("a question has at most %d marking-scheme passages", maxExcerpts)
	}
	for _, e := range excerpts {
		switch {
		case strings.TrimSpace(e.Text) == "" || utf8.RuneCountInString(e.Text) > maxExcerptText:
			return fmt.Errorf("each marking-scheme passage needs 1–%d characters of text", maxExcerptText)
		case e.Document != "" && !platform.ID(e.Document), e.Chunk != "" && !platform.ID(e.Chunk), e.Chunk != "" && e.Document == "":
			return errors.New("a marking-scheme passage names its document and chunk by id")
		case e.Page < 0 || e.Page > maxExcerptPage:
			return errors.New("a marking-scheme passage's page is out of range")
		}
	}
	return nil
}

func (c Criterion) validate() error {
	if strings.TrimSpace(c.Title) == "" || utf8.RuneCountInString(c.Title) > maxCriterion {
		return errors.New("each rubric criterion needs a title")
	}
	if len(c.Levels) < minLevels || len(c.Levels) > maxLevels {
		return fmt.Errorf("each rubric criterion needs %d–%d levels", minLevels, maxLevels)
	}
	seen := map[int]bool{}
	for _, l := range c.Levels {
		if l.Points < 0 || l.Points > maxPoints || seen[l.Points] || strings.TrimSpace(l.Descriptor) == "" || utf8.RuneCountInString(l.Descriptor) > maxDescriptor {
			return errors.New("each level needs a description and its own points from 0 to 100")
		}
		seen[l.Points] = true
	}
	return nil
}

func (freeResponse) accepts(q Question, answer string) bool {
	return strings.TrimSpace(answer) != "" && utf8.RuneCountInString(answer) <= q.MaxLength
}

func (freeResponse) limit(q Question) int { return q.MaxLength }

// top is the most a criterion can award: its highest level.
func (c Criterion) top() int {
	best := 0
	for _, l := range c.Levels {
		best = max(best, l.Points)
	}
	return best
}

// total is the most a rubric can award.
func (r Rubric) total() int {
	sum := 0
	for _, c := range r.Criteria {
		sum += c.top()
	}
	return sum
}
