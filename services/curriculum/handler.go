package main

import (
	"errors"
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
	if !nodeID(w, r) {
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
		platform.Invalid(w, "Provide a supported kind, a name of 1–160 characters and an optional parent UUID.")
		return
	}
	org := platform.Actor(r).Org
	// A node may stand alone; a parent, when given, must be the level above it.
	parentOK := v.Parent == nil
	if v.Parent != nil {
		kind, err := s.store.findKind(r.Context(), org, *v.Parent)
		if err != nil && !errors.Is(err, platform.ErrNotFound) {
			platform.Fail(w, err)
			return
		}
		parentOK = err == nil && expectedParent[v.Kind] == kind
	}
	if !parentOK {
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PARENT", "Parent must be the preceding curriculum level in your organization.")
		return
	}
	id, name := uuid.NewString(), strings.TrimSpace(v.Name)
	if err := s.store.insertNode(r.Context(), id, org, v.Kind, name, v.Parent); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, Node{id, v.Kind, name, v.Parent})
}

func (s server) rename(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !nodeID(w, r) {
		return
	}
	var v struct {
		Name string `json:"name"`
	}
	if !platform.Decode(w, r, &v) {
		return
	}
	if !validName(v.Name) {
		platform.Invalid(w, "Provide a name of 1–160 characters.")
		return
	}
	name := strings.TrimSpace(v.Name)
	updated, err := s.store.renameNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"), name)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !updated {
		platform.NotFound(w, "Node not found.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"id": r.PathValue("id"), "name": name})
}

func (s server) archive(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !nodeID(w, r) {
		return
	}
	result, err := s.store.archiveNode(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	switch result {
	case archiveMissing:
		platform.NotFound(w, "Node not found.")
		return
	case archiveHasChildren:
		platform.Error(w, http.StatusConflict, "ARCHIVE_CONFLICT", "Archive the node's children first.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// nodeID reports whether the path's node id is a UUID, answering 404 like a missing node when it is not.
func nodeID(w http.ResponseWriter, r *http.Request) bool {
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Node not found.")
		return false
	}
	return true
}

func (s server) addPrerequisite(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !nodeID(w, r) {
		return
	}
	var input struct {
		Prerequisite string `json:"prerequisite_id"`
	}
	if !platform.Decode(w, r, &input) {
		return
	}
	id := r.PathValue("id")
	if !platform.ID(input.Prerequisite) || id == input.Prerequisite {
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
