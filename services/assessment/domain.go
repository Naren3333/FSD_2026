package main

import (
	"errors"
	"learning/platform"
	"slices"
	"strings"
)

// Assessment limits.
const (
	maxTitle     = 160
	maxQuestions = 50
	maxPrompt    = 2000
	minOptions   = 2
	maxOptions   = 8
	maxAnswer    = 500 // also the longest option
)

type Question struct {
	ID        string   `json:"id"`
	Prompt    string   `json:"prompt"`
	Type      string   `json:"type"`
	Options   []string `json:"options"`
	Answer    string   `json:"answer"`
	Tolerance string   `json:"tolerance"`
	Skill     string   `json:"skill_id"`
}
type Assessment struct {
	ID        string     `json:"id"`
	Classroom string     `json:"classroom_id"`
	Title     string     `json:"title"`
	Questions []Question `json:"questions"`
}

// Summary is an assessment in a classroom's list, with whether the caller has submitted it.
type Summary struct {
	ID        string `json:"id"`
	Title     string `json:"title"`
	Submitted bool   `json:"submitted"`
}

// Progress is how many students have submitted an assessment, for its classroom's teacher.
type Progress struct {
	ID          string `json:"id"`
	Title       string `json:"title"`
	Submissions int    `json:"submissions"`
}

// Submission is one student's answers to an assessment, by question id.
type Submission struct {
	ID, Org, Student string
	Answers          map[string]string
}

type PublicQuestion struct {
	ID      string   `json:"id"`
	Prompt  string   `json:"prompt"`
	Type    string   `json:"type"`
	Options []string `json:"options"`
	Skill   string   `json:"skill_id"`
}
type PublicAssessment struct {
	ID        string           `json:"id"`
	Classroom string           `json:"classroom_id"`
	Title     string           `json:"title"`
	Questions []PublicQuestion `json:"questions"`
}

func (v Assessment) Validate() error {
	if !platform.ID(v.Classroom) || strings.TrimSpace(v.Title) == "" || len(v.Title) > maxTitle || len(v.Questions) < 1 || len(v.Questions) > maxQuestions {
		return errors.New("provide a classroom, title, and 1–50 questions")
	}
	for _, q := range v.Questions {
		if err := q.validate(); err != nil {
			return err
		}
	}
	return nil
}

func (q Question) validate() error {
	if strings.TrimSpace(q.Prompt) == "" || len(q.Prompt) > maxPrompt || !platform.ID(q.Skill) {
		return errors.New("each question needs a prompt and curriculum skill")
	}
	kind, ok := questionKinds[q.Type]
	if !ok {
		return errors.New("unsupported question type")
	}
	return kind.validate(q)
}

// skillIDs are the distinct skills the questions assess.
func (v Assessment) skillIDs() []string {
	var ids []string
	for _, q := range v.Questions {
		if !slices.Contains(ids, q.Skill) {
			ids = append(ids, q.Skill)
		}
	}
	return ids
}

func (v Assessment) Public() PublicAssessment {
	p := PublicAssessment{v.ID, v.Classroom, v.Title, []PublicQuestion{}}
	for _, q := range v.Questions {
		p.Questions = append(p.Questions, PublicQuestion{q.ID, q.Prompt, q.Type, q.Options, q.Skill})
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
		if !exists || !known || len(answer) > maxAnswer || !kind.accepts(q, answer) {
			return false
		}
	}
	return true
}
