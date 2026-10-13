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
