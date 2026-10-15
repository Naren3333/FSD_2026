package main

import (
	"learning/platform"
	"net/http"
	"strings"

	"github.com/google/uuid"
)

// server answers Curriculum's HTTP routes from its store.
type server struct{ store store }

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("GET /v1/nodes", s.list)
	mux.HandleFunc("GET /v1/nodes/{id}", s.get)
	mux.HandleFunc("POST /v1/nodes", s.create)
	mux.HandleFunc("PATCH /v1/nodes/{id}", s.rename)
	mux.HandleFunc("DELETE /v1/nodes/{id}", s.archive)
	mux.HandleFunc("POST /v1/skills/{id}/prerequisites", s.addPrerequisite)
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	result, err := s.store.listNodes(r.Context(), platform.Actor(r).Org)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Node not found.")
		return
	}
	v, err := s.store.findNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Node not found.") {
		return
	}
	platform.JSON(w, http.StatusOK, v)
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
		platform.Invalid(w, "Provide a supported kind, name and optional parent UUID.")
		return
	}
	org := platform.Actor(r).Org
	if v.Parent != nil {
		kind, err := s.store.findKind(r.Context(), org, *v.Parent)
		if err != nil || expectedParent[v.Kind] != kind {
			platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PARENT", "Parent must be the preceding curriculum level in your organization.")
			return
		}
	}
	id := uuid.NewString()
	if err := s.store.insertNode(r.Context(), id, org, v.Kind, strings.TrimSpace(v.Name), v.Parent); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, Node{id, v.Kind, v.Name, v.Parent})
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
	if !platform.ID(r.PathValue("id")) || !validName(v.Name) {
		platform.Invalid(w, "Provide an identifier and a name of 1–160 characters.")
		return
	}
	updated, err := s.store.renameNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"), strings.TrimSpace(v.Name))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !updated {
		platform.NotFound(w, "Node not found.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"id": r.PathValue("id"), "name": v.Name})
}

func (s server) archive(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Node not found.")
		return
	}
	archived, err := s.store.archiveNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !archived {
		platform.Error(w, http.StatusConflict, "ARCHIVE_CONFLICT", "Node is absent, already archived, or has active children.")
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
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PREREQUISITE", "Choose a different skill.")
		return
	}
	result, err := s.store.insertPrerequisite(r.Context(), platform.Actor(r).Org, id, input.Prerequisite)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	switch result {
	case prerequisiteInvalid:
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PREREQUISITE", "Both skills must be active in your organization.")
		return
	case prerequisiteCycle:
		platform.Error(w, http.StatusConflict, "PREREQUISITE_CYCLE", "This prerequisite would create a cycle.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"skill_id": id, "prerequisite_id": input.Prerequisite})
}
