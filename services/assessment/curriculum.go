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

// check all topic references; prioritize authentication failures, then outages, then invalid topics.
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

// curriculum asks the curriculum service, forwarding the caller's token.
type curriculum struct {
	base   string
	client *http.Client
}

// check asks the curriculum service whether the topic exists in the caller's curriculum: a 200 with the
// topic is valid.
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
		// assessment has already checked the caller teaches the classroom, so curriculum refusing them is a
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
