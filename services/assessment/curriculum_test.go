package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// answersBySkill answers each skill from a table; a skill not in it is invalid.
type answersBySkill map[string]skillStatus

func (a answersBySkill) check(_ *http.Request, skill string) skillStatus {
	if status, ok := a[skill]; ok {
		return status
	}
	return skillInvalid
}

func TestCheckAllReportsTheWorstAnswer(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/", nil)
	answers := answersBySkill{"a": skillValid, "b": skillValid, "down": skillUnavailable}
	cases := []struct {
		ids  []string
		want skillStatus
	}{
		{[]string{"a", "b"}, skillValid},
		{[]string{"a", "missing"}, skillInvalid},
		{[]string{"missing", "down", "a"}, skillUnavailable},
		{nil, skillValid},
	}
	for _, tc := range cases {
		if got := checkAll(r, answers, tc.ids); got != tc.want {
			t.Errorf("%v: got %d, want %d", tc.ids, got, tc.want)
		}
	}
}

func TestSkillIDsAreDistinct(t *testing.T) {
	v := Assessment{Questions: []Question{{Skill: "a"}, {Skill: "b"}, {Skill: "a"}}}
	if got := v.skillIDs(); len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("got %v", got)
	}
}

func TestCheckMapsCurriculumAnswers(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   skillStatus
	}{
		{"a skill", http.StatusOK, `{"id":"s","kind":"skill"}`, skillValid},
		{"not a skill", http.StatusOK, `{"id":"s","kind":"topic"}`, skillInvalid},
		{"missing", http.StatusNotFound, `{"error":{}}`, skillInvalid},
		{"refused", http.StatusForbidden, `{}`, skillInvalid},
		{"curriculum failing", http.StatusInternalServerError, `{}`, skillUnavailable},
		{"curriculum unavailable", http.StatusServiceUnavailable, ``, skillUnavailable},
		{"unreadable body", http.StatusOK, `<html>`, skillUnavailable},
	}
	for _, tc := range cases {
		var auth string
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			auth = r.Header.Get("Authorization")
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		r := httptest.NewRequest(http.MethodPost, "/", nil)
		r.Header.Set("Authorization", "Bearer caller")
		got := curriculum{server.URL, server.Client()}.check(r, skill)
		server.Close()
		if got != tc.want || auth != "Bearer caller" {
			t.Errorf("%s: got %d (token %q), want %d", tc.name, got, auth, tc.want)
		}
	}
}

func TestCheckIsUnavailableWhenCurriculumIsUnreachable(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	server.Close()
	if got := (curriculum{server.URL, server.Client()}).check(httptest.NewRequest(http.MethodPost, "/", nil), skill); got != skillUnavailable {
		t.Fatalf("got %d", got)
	}
}
