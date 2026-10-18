package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// answersByTopic answers each topic from a table; a topic not in it is invalid.
type answersByTopic map[string]topicStatus

func (a answersByTopic) check(_ *http.Request, topic string) topicStatus {
	if status, ok := a[topic]; ok {
		return status
	}
	return topicInvalid
}

func TestCheckAllReportsTheWorstAnswer(t *testing.T) {
	r := httptest.NewRequest(http.MethodPost, "/", nil)
	answers := answersByTopic{"a": topicValid, "b": topicValid, "down": topicUnavailable, "expired": topicUnauthenticated}
	cases := []struct {
		ids  []string
		want topicStatus
	}{
		{[]string{"a", "b"}, topicValid},
		{[]string{"a", "missing"}, topicInvalid},
		{[]string{"missing", "down", "a"}, topicUnavailable},
		{[]string{"down", "expired", "missing"}, topicUnauthenticated},
		{nil, topicValid},
	}
	for _, tc := range cases {
		if got := checkAll(r, answers, tc.ids); got != tc.want {
			t.Errorf("%v: got %d, want %d", tc.ids, got, tc.want)
		}
	}
}

func TestTopicIDsAreDistinct(t *testing.T) {
	v := Assessment{Questions: []Question{{Topic: "a"}, {Topic: "b"}, {Topic: "a"}}}
	if got := v.topicIDs(); len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Fatalf("got %v", got)
	}
}

func TestCheckMapsCurriculumAnswers(t *testing.T) {
	cases := []struct {
		name   string
		status int
		body   string
		want   topicStatus
	}{
		{"a topic", http.StatusOK, `{"id":"s","name":"Fractions"}`, topicValid},
		{"no topic in the answer", http.StatusOK, `{}`, topicUnavailable},
		{"missing", http.StatusNotFound, `{"error":{}}`, topicInvalid},
		{"token not accepted", http.StatusUnauthorized, `{}`, topicUnauthenticated},
		{"refused", http.StatusForbidden, `{}`, topicUnavailable},
		{"curriculum failing", http.StatusInternalServerError, `{}`, topicUnavailable},
		{"curriculum unavailable", http.StatusServiceUnavailable, ``, topicUnavailable},
		{"unreadable body", http.StatusOK, `<html>`, topicUnavailable},
	}
	for _, tc := range cases {
		var auth, path string
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			auth, path = r.Header.Get("Authorization"), r.URL.Path
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		r := httptest.NewRequest(http.MethodPost, "/", nil)
		r.Header.Set("Authorization", "Bearer caller")
		got := curriculum{server.URL, server.Client()}.check(r, topic)
		server.Close()
		if got != tc.want || auth != "Bearer caller" || path != "/v1/topics/"+topic {
			t.Errorf("%s: got %d (token %q, path %q), want %d", tc.name, got, auth, path, tc.want)
		}
	}
}

func TestCheckIsUnavailableWhenCurriculumIsUnreachable(t *testing.T) {
	server := httptest.NewServer(http.NotFoundHandler())
	server.Close()
	if got := (curriculum{server.URL, server.Client()}).check(httptest.NewRequest(http.MethodPost, "/", nil), topic); got != topicUnavailable {
		t.Fatalf("got %d", got)
	}
}
