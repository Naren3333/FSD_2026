package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
)

// the memory store's question bank and resits, as pgStore decides them (adr 0017).

type memoryBankQuestion struct {
	BankQuestion
	owner string
}

// banked is q as a bank question, its id q's, saved at now.
func banked(q Question, now time.Time) BankQuestion {
	data, _ := json.Marshal(q)
	var b BankQuestion
	_ = json.Unmarshal(data, &b)
	b.CreatedAt, b.UpdatedAt = now, now
	return b
}

func (m *memoryStore) owned(id, owner string) bool {
	b, ok := m.bank[id]
	return ok && b.owner == owner
}

func (m *memoryStore) writeLinks(v Assessment) {
	if m.links == nil {
		m.links = map[string]map[string]string{}
	}
	m.links[v.ID] = map[string]string{}
	for i, bank := range v.BankLinks {
		if i < len(v.Questions) && m.owned(bank, v.LinkOwner) {
			m.links[v.ID][v.Questions[i].ID] = bank
		}
	}
}

func (m *memoryStore) findLinks(_ context.Context, _, assessment, owner string) (map[string]string, error) {
	links := map[string]string{}
	for question, bank := range m.links[assessment] {
		if m.owned(bank, owner) {
			links[question] = bank
		}
	}
	return links, nil
}

func (m *memoryStore) createResit(_ context.Context, _ string, v Assessment) error {
	for _, bank := range v.BankLinks {
		if bank != "" && !m.owned(bank, v.LinkOwner) {
			return errInvalidBankQuestion
		}
	}
	v.Assignees = slices.Sorted(slices.Values(v.Assignees))
	m.assessments[v.ID] = v
	m.writeLinks(v)
	return nil
}

func (m *memoryStore) setAssignees(_ context.Context, _, id string, students, checked []string) error {
	v, ok := m.assessments[id]
	switch {
	case !ok:
		return platform.ErrNotFound
	case !v.Assigned:
		return errNotAssigned
	}
	_, removed, err := assigneeChanges(v.Assignees, students, checked)
	if err != nil {
		return err
	}
	for _, student := range removed {
		if _, done := m.submitted[id+"/"+student]; done {
			return errHasSubmissions
		}
	}
	v.Assignees = slices.Sorted(slices.Values(students))
	m.assessments[id] = v
	return nil
}

func (m *memoryStore) insertBankQuestion(_ context.Context, _, owner string, q Question, from *bankSource) (string, error) {
	if m.bank == nil {
		m.bank = map[string]memoryBankQuestion{}
	}
	held := 0
	for _, b := range m.bank {
		if b.owner == owner {
			held++
		}
	}
	if held >= maxBank {
		return "", errBankFull
	}
	if from != nil {
		v, ok := m.assessments[from.assessment]
		if !ok {
			return "", platform.ErrNotFound
		}
		if !slices.ContainsFunc(v.Questions, func(q Question) bool { return q.ID == from.question }) {
			return "", errAssessmentChanged
		}
		if bank := m.links[from.assessment][from.question]; m.owned(bank, owner) {
			return "", errAlreadyInBank{bank}
		}
	}
	q = bankCopy(q)
	q.ID = uuid.NewString()
	now := time.Now()
	m.bank[q.ID] = memoryBankQuestion{banked(q, now), owner}
	if from != nil {
		if m.links == nil {
			m.links = map[string]map[string]string{}
		}
		if m.links[from.assessment] == nil {
			m.links[from.assessment] = map[string]string{}
		}
		m.links[from.assessment][from.question] = q.ID
	}
	return q.ID, nil
}

func (m *memoryStore) updateBankQuestion(_ context.Context, _, owner, id string, q Question) error {
	if !m.owned(id, owner) {
		return platform.ErrNotFound
	}
	b := m.bank[id]
	q.ID = id
	created := b.CreatedAt
	b.BankQuestion = banked(bankCopy(q), time.Now())
	b.ID, b.CreatedAt = id, created
	m.bank[id] = b
	return nil
}

func (m *memoryStore) deleteBankQuestion(_ context.Context, _, owner, id string) error {
	if !m.owned(id, owner) {
		return platform.ErrNotFound
	}
	delete(m.bank, id)
	return nil
}

func (m *memoryStore) findBankQuestion(_ context.Context, _, owner, id string) (BankQuestion, error) {
	if !m.owned(id, owner) {
		return BankQuestion{}, platform.ErrNotFound
	}
	return m.bank[id].BankQuestion, nil
}

func (m *memoryStore) findBankQuestions(_ context.Context, _, owner string, ids []string) ([]BankQuestion, error) {
	var items []BankQuestion
	for _, id := range ids {
		if m.owned(id, owner) {
			items = append(items, m.bank[id].BankQuestion)
		}
	}
	return items, nil
}

func (m *memoryStore) listBank(_ context.Context, _, owner string, f bankFilter, window platform.ListWindow) ([]BankQuestion, int, error) {
	var items []BankQuestion
	for _, b := range m.bank {
		if b.owner != owner || f.kind != "" && b.Type != f.kind || len(f.topics) > 0 && !slices.Contains(f.topics, b.Topic) ||
			f.text != "" && !strings.Contains(strings.ToLower(b.Prompt), strings.ToLower(f.text)) {
			continue
		}
		item := b.BankQuestion
		if f.student != "" {
			answered := false
			for assessment, links := range m.links {
				_, done := m.submitted[assessment+"/"+f.student]
				answered = answered || done && slices.Contains(slices.Collect(maps.Values(links)), b.ID)
			}
			if answered && f.excludeAnswered {
				continue
			}
			item.Answered = &answered
		}
		items = append(items, item)
	}
	slices.SortFunc(items, func(a, b BankQuestion) int { return b.UpdatedAt.Compare(a.UpdatedAt) })
	total := len(items)
	items = items[min(window.Offset, total):min(window.Offset+window.Limit, total)]
	return items, total, nil
}
