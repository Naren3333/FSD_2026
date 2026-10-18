package main

import (
	"encoding/json"
	"io"
	"learning/platform"
	"log/slog"
	"net/http"
	"sync"
)

type topicStatus int

const (
	topicValid topicStatus = iota
	topicInvalid
	topicUnavailable
	topicUnauthenticated
)

// topics checks that a topic exists in the caller's curriculum.
type topics interface {
	check(r *http.Request, topic string) topicStatus
}

// checkAll checks every topic at once and reports the worst answer: unauthenticated if Curriculum refused the
// caller's token for any of them, otherwise unavailable if it couldn't answer for any, otherwise invalid if any is not
// a topic.
func checkAll(r *http.Request, s topics, ids []string) topicStatus {
	statuses := make([]topicStatus, len(ids))
	var wg sync.WaitGroup
	for i, id := range ids {
		wg.Go(func() { statuses[i] = s.check(r, id) })
	}
	wg.Wait()
	worst := topicValid
	for _, status := range statuses {
		worst = max(worst, status)
	}
	return worst
}

// curriculum asks the Curriculum service, forwarding the caller's token.
type curriculum struct {
	base   string
	client *http.Client
}

// check asks the curriculum service whether the topic exists in the caller's curriculum: a 200 with the topic is
// valid. A topic Curriculum says is missing (any other 4xx) is invalid. A token it does not accept (401) is
// unauthenticated, so the teacher signs in again; a refusal (403), Curriculum failing (5xx) or a body it cannot read
// is unavailable. Neither an expired token nor an outage is ever reported to the teacher as an invalid topic.
func (c curriculum) check(r *http.Request, topic string) topicStatus {
	req, err := platform.Forward(r, http.MethodGet, c.base+"/v1/topics/"+topic)
	if err != nil {
		slog.Error("curriculum_request_invalid", "error", err.Error())
		return topicUnavailable
	}
	res, err := c.client.Do(req)
	if err != nil {
		return topicUnavailable
	}
	defer res.Body.Close()
	switch {
	case res.StatusCode >= http.StatusInternalServerError:
		return topicUnavailable
	case res.StatusCode == http.StatusUnauthorized:
		return topicUnauthenticated
	case res.StatusCode == http.StatusForbidden:
		// Assessment has already checked the caller teaches the classroom, so Curriculum refusing them is a
		// misconfiguration to fix, not a topic the teacher picked wrongly.
		slog.Warn("curriculum_refused_topic_check", "topic", topic)
		return topicUnavailable
	case res.StatusCode != http.StatusOK:
		return topicInvalid
	}
	var found struct {
		ID string `json:"id"`
	}
	if json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&found) != nil || found.ID == "" {
		return topicUnavailable
	}
	return topicValid
}
