package main

import (
	"encoding/json"
	"io"
	"learning/platform"
	"log/slog"
	"net/http"
	"sync"
)

type skillStatus int

const (
	skillValid skillStatus = iota
	skillInvalid
	skillUnavailable
)

// skills checks that a node is a skill in the caller's curriculum.
type skills interface {
	check(r *http.Request, skill string) skillStatus
}

// checkAll checks every skill at once and reports the worst answer: unavailable if Curriculum couldn't answer for
// any of them, otherwise invalid if any is not a skill.
func checkAll(r *http.Request, s skills, ids []string) skillStatus {
	statuses := make([]skillStatus, len(ids))
	var wg sync.WaitGroup
	for i, id := range ids {
		wg.Go(func() { statuses[i] = s.check(r, id) })
	}
	wg.Wait()
	worst := skillValid
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

// check asks the curriculum service whether the node exists and is a skill. A node Curriculum says is missing or
// refuses (4xx) is invalid; Curriculum failing (5xx) or answering with a body it cannot read is unavailable, so an
// outage is never reported to the teacher as an invalid skill.
func (c curriculum) check(r *http.Request, skill string) skillStatus {
	req, err := platform.Forward(r, http.MethodGet, c.base+"/v1/nodes/"+skill)
	if err != nil {
		slog.Error("curriculum_request_invalid", "error", err.Error())
		return skillUnavailable
	}
	res, err := c.client.Do(req)
	if err != nil {
		return skillUnavailable
	}
	defer res.Body.Close()
	switch {
	case res.StatusCode >= http.StatusInternalServerError:
		return skillUnavailable
	case res.StatusCode != http.StatusOK:
		return skillInvalid
	}
	var node struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
	}
	if json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&node) != nil {
		return skillUnavailable
	}
	if node.Kind != "skill" {
		return skillInvalid
	}
	return skillValid
}
