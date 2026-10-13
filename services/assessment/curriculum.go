package main

import (
	"encoding/json"
	"learning/platform"
	"log/slog"
	"net/http"
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

// curriculum asks the Curriculum service, forwarding the caller's token.
type curriculum struct {
	base   string
	client *http.Client
}

// check asks the curriculum service whether the node exists and is a skill.
func (c curriculum) check(r *http.Request, skill string) skillStatus {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, c.base+"/v1/nodes/"+skill, nil)
	if err != nil {
		slog.Error("curriculum_request_invalid", "error", err.Error())
		return skillUnavailable
	}
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	req.Header.Set("X-Correlation-ID", platform.Correlation(r.Context()))
	res, err := c.client.Do(req)
	if err != nil {
		return skillUnavailable
	}
	var node struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
	}
	decoded := json.NewDecoder(res.Body).Decode(&node) == nil
	res.Body.Close()
	if res.StatusCode != http.StatusOK || !decoded || node.Kind != "skill" {
		return skillInvalid
	}
	return skillValid
}
