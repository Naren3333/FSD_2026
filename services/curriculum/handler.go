package main

import (
	"errors"
	"learning/platform"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type server struct{ *platform.App }

func (s server) list(w http.ResponseWriter, r *http.Request) {
	result, err := s.listNodes(r.Context(), platform.Actor(r).Org)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Node not found.")
		return
	}
	v, err := s.findNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Node not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, v)
}

func (s server) create(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var v Input
	if !platform.Decode(w, r, &v) {
		return
	}
	if !valid(v) {
		platform.Error(w, 422, "VALIDATION_ERROR", "Provide a supported kind, name and optional parent UUID.")
		return
	}
	org := platform.Actor(r).Org
	if v.Parent != nil {
		kind, err := s.findKind(r.Context(), org, *v.Parent)
		if err != nil || expectedParent[v.Kind] != kind {
			platform.Error(w, 422, "INVALID_PARENT", "Parent must be the preceding curriculum level in your organization.")
			return
		}
	}
	id := uuid.NewString()
	if err := s.insertNode(r.Context(), id, org, v.Kind, strings.TrimSpace(v.Name), v.Parent); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 201, Node{id, v.Kind, v.Name, v.Parent})
}

func (s server) rename(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var v struct {
		Name string `json:"name"`
	}
	if !platform.Decode(w, r, &v) {
		return
	}
	if !platform.ID(r.PathValue("id")) || strings.TrimSpace(v.Name) == "" || len(v.Name) > 160 {
		platform.Error(w, 422, "VALIDATION_ERROR", "Provide an identifier and a name of 1–160 characters.")
		return
	}
	updated, err := s.renameNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"), strings.TrimSpace(v.Name))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !updated {
		platform.Error(w, 404, "NOT_FOUND", "Node not found.")
		return
	}
	platform.JSON(w, 200, map[string]string{"id": r.PathValue("id"), "name": v.Name})
}

func (s server) archive(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Node not found.")
		return
	}
	archived, err := s.archiveNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !archived {
		platform.Error(w, 409, "ARCHIVE_CONFLICT", "Node is absent, already archived, or has active children.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s server) addPrerequisite(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	var input struct {
		Prerequisite string `json:"prerequisite_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	id := r.PathValue("id")
	if !platform.ID(id) || !platform.ID(input.Prerequisite) || id == input.Prerequisite {
		platform.Error(w, 422, "INVALID_PREREQUISITE", "Choose a different skill.")
		return
	}
	result, err := s.insertPrerequisite(r.Context(), platform.Actor(r).Org, id, input.Prerequisite)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	switch result {
	case prerequisiteInvalid:
		platform.Error(w, 422, "INVALID_PREREQUISITE", "Both skills must be active in your organization.")
		return
	case prerequisiteCycle:
		platform.Error(w, 409, "PREREQUISITE_CYCLE", "This prerequisite would create a cycle.")
		return
	}
	platform.JSON(w, 200, map[string]string{"skill_id": id, "prerequisite_id": input.Prerequisite})
}
