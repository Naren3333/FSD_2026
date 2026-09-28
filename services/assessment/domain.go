package main

import (
	"errors"
	"learning/platform"
	"regexp"
	"strings"
)

var decimal = regexp.MustCompile(`^-?[0-9]{1,12}(\.[0-9]{1,8})?$`)

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
	if !platform.ID(v.Classroom) || strings.TrimSpace(v.Title) == "" || len(v.Title) > 160 || len(v.Questions) < 1 || len(v.Questions) > 50 {
		return errors.New("provide a classroom, title, and 1–50 questions")
	}
	for _, q := range v.Questions {
		if strings.TrimSpace(q.Prompt) == "" || len(q.Prompt) > 2000 || !platform.ID(q.Skill) {
			return errors.New("each question needs a prompt and curriculum skill")
		}
		switch q.Type {
		case "multiple_choice":
			if len(q.Options) < 2 || len(q.Options) > 8 {
				return errors.New("multiple choice needs 2–8 options")
			}
			found := false
			seen := map[string]bool{}
			for _, o := range q.Options {
				if strings.TrimSpace(o) == "" || len(o) > 500 || seen[o] {
					return errors.New("options must be unique nonempty values")
				}
				seen[o] = true
				if o == q.Answer {
					found = true
				}
			}
			if !found {
				return errors.New("answer must match one option")
			}
			if q.Tolerance != "" {
				return errors.New("multiple choice has no tolerance")
			}
		case "numerical":
			if !decimal.MatchString(q.Answer) || !decimal.MatchString(q.Tolerance) || strings.HasPrefix(q.Tolerance, "-") || len(q.Options) > 0 {
				return errors.New("numerical answers require decimal answer and nonnegative decimal tolerance, without options")
			}
		default:
			return errors.New("unsupported question type")
		}
	}
	return nil
}
func (v Assessment) Public() PublicAssessment {
	p := PublicAssessment{v.ID, v.Classroom, v.Title, []PublicQuestion{}}
	for _, q := range v.Questions {
		p.Questions = append(p.Questions, PublicQuestion{q.ID, q.Prompt, q.Type, q.Options, q.Skill})
	}
	return p
}
func validateAnswers(v Assessment, answers map[string]string) bool {
	if len(answers) != len(v.Questions) {
		return false
	}
	for _, q := range v.Questions {
		answer, exists := answers[q.ID]
		if !exists || len(answer) > 500 {
			return false
		}
		if q.Type == "numerical" && !decimal.MatchString(answer) {
			return false
		}
		if q.Type == "multiple_choice" {
			found := false
			for _, o := range q.Options {
				if answer == o {
					found = true
				}
			}
			if !found {
				return false
			}
		}
	}
	return true
}
