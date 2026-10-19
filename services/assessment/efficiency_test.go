package main

import (
	"fmt"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"strings"
	"testing"
)

func TestBatchReadsUseTheSameStudentProjectionAndMembershipAsGet(t *testing.T) {
	s, v := created(t, platform.RelationStudent)
	body := fmt.Sprintf(`{"ids":[%q]}`, v.ID)
	w := call(t, s, http.MethodPost, "/v1/assessments/batch", body, "student")
	if w.Code != http.StatusOK || strings.Contains(w.Body.String(), `"answer"`) || strings.Contains(w.Body.String(), `"rubric"`) || !strings.Contains(w.Body.String(), v.ID) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access("")
	if w = call(t, s, http.MethodPost, "/v1/assessments/batch", body, "student"); w.Code != http.StatusForbidden {
		t.Fatalf("membership: %d", w.Code)
	}
}

func TestBatchRejectsDuplicatesAndNeverReturnsAPartialUnknownResult(t *testing.T) {
	s, v := created(t, platform.RelationTeacher)
	for _, body := range []string{fmt.Sprintf(`{"ids":[%q,%q]}`, v.ID, v.ID), `{"ids":[]}`} {
		if w := call(t, s, http.MethodPost, "/v1/assessments/batch", body, "teacher"); w.Code != http.StatusUnprocessableEntity {
			t.Fatalf("%d %s", w.Code, w.Body.String())
		}
	}
	body := fmt.Sprintf(`{"ids":[%q,"11111111-1111-4111-8111-111111111111"]}`, v.ID)
	w := call(t, s, http.MethodPost, "/v1/assessments/batch", body, "teacher")
	if w.Code != http.StatusNotFound || strings.Contains(w.Body.String(), v.Title) {
		t.Fatalf("partial data: %d %s", w.Code, w.Body.String())
	}
}
