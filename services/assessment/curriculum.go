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
)

// topics checks that a topic exists in the caller's curriculum.
type topics interface {
	check(r *http.Request, topic string) topicStatus
}

// checkAll checks every topic at once and reports the worst answer: unavailable if Curriculum couldn't answer for
// any of them, otherwise invalid if any is not a topic.
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
// valid. A topic Curriculum says is missing or refuses (4xx) is invalid; Curriculum failing (5xx) or answering with
// a body it cannot read is unavailable, so an outage is never reported to the teacher as an invalid topic.
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
