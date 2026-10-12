package main

import (
	"encoding/json"
	"learning/platform"
	"net/http"
)

type skillStatus int

const (
	skillValid skillStatus = iota
	skillInvalid
	skillUnavailable
)

// checkSkill asks the curriculum service whether the node exists and is a skill.
// A non-nil error means the request could not even be built.
func (s server) checkSkill(r *http.Request, skill string) (skillStatus, error) {
	req, err := http.NewRequestWithContext(r.Context(), "GET", platform.Required("CURRICULUM_URL")+"/v1/nodes/"+skill, nil)
	if err != nil {
		return skillInvalid, err
	}
	req.Header.Set("Authorization", r.Header.Get("Authorization"))
	req.Header.Set("X-Correlation-ID", platform.Correlation(r.Context()))
	res, err := s.Client.Do(req)
	if err != nil {
		return skillUnavailable, nil
	}
	var node struct {
		ID   string `json:"id"`
		Kind string `json:"kind"`
	}
	err = json.NewDecoder(res.Body).Decode(&node)
	res.Body.Close()
	if res.StatusCode != 200 || err != nil || node.Kind != "skill" {
		return skillInvalid, nil
	}
	return skillValid, nil
}
