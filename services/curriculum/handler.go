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
	mux.HandleFunc("GET /v1/topics", s.list)
	mux.HandleFunc("GET /v1/topics/{id}", s.get)
	mux.HandleFunc("POST /v1/topics", s.create)
	mux.HandleFunc("PATCH /v1/topics/{id}", s.rename)
	mux.HandleFunc("DELETE /v1/topics/{id}", s.archive)
	mux.HandleFunc("POST /v1/topics/{id}/restore", s.restore)
	mux.HandleFunc("GET /v1/topics/{id}/prerequisites", s.prerequisites)
	mux.HandleFunc("POST /v1/topics/{id}/prerequisites", s.addPrerequisite)
	mux.HandleFunc("DELETE /v1/topics/{id}/prerequisites/{prerequisite}", s.removePrerequisite)
}

// list answers the active topics, or with ?archived=true the archived ones, which pages still need to name
// existing questions and evidence.
func (s server) list(w http.ResponseWriter, r *http.Request) {
	archived := r.URL.Query().Get("archived")
	if archived != "" && archived != "true" && archived != "false" {
		platform.Invalid(w, "archived must be true or false.")
		return
	}
	result, err := s.store.listTopics(r.Context(), platform.Actor(r).Org, archived == "true")
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	if !topicID(w, r) {
		return
	}
	v, err := s.store.findTopic(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Topic not found.") {
		return
	}
	platform.JSON(w, http.StatusOK, v)
}

// named decodes a body of just a name and checks it, answering the failure itself.
func named(w http.ResponseWriter, r *http.Request) (string, bool) {
	var v struct {
		Name string `json:"name"`
	}
	if !platform.Decode(w, r, &v) {
		return "", false
	}
	if !validName(v.Name) {
		platform.Invalid(w, "Provide a name of 1–160 characters.")
		return "", false
	}
	return strings.TrimSpace(v.Name), true
}

func (s server) create(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	name, ok := named(w, r)
	if !ok {
		return
	}
	id := uuid.NewString()
	if err := s.store.insertTopic(r.Context(), id, platform.Actor(r).Org, name); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, Topic{id, name})
}

func (s server) rename(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !topicID(w, r) {
		return
	}
	name, ok := named(w, r)
	if !ok {
		return
	}
	updated, err := s.store.renameTopic(r.Context(), platform.Actor(r).Org, r.PathValue("id"), name)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !updated {
		platform.NotFound(w, "Topic not found.")
		return
	}
	platform.JSON(w, http.StatusOK, Topic{r.PathValue("id"), name})
}

func (s server) archive(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !topicID(w, r) {
		return
	}
	archived, err := s.store.archiveTopic(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !archived {
		platform.NotFound(w, "Topic not found.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (s server) restore(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !topicID(w, r) {
		return
	}
	v, err := s.store.restoreTopic(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if !platform.Found(w, err, "Archived topic not found.") {
		return
	}
	platform.JSON(w, http.StatusOK, v)
}

// topicID reports whether the path's topic id is a UUID, answering 404 like a missing topic when it is not.
func topicID(w http.ResponseWriter, r *http.Request) bool {
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Topic not found.")
		return false
	}
	return true
}

func (s server) addPrerequisite(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !topicID(w, r) {
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
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PREREQUISITE", "Choose a different topic.")
		return
	}
	result, err := s.store.insertPrerequisite(r.Context(), platform.Actor(r).Org, id, input.Prerequisite)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	switch result {
	case prerequisiteInvalid:
		platform.Error(w, http.StatusUnprocessableEntity, "INVALID_PREREQUISITE", "Both topics must be active in your organization.")
		return
	case prerequisiteCycle:
		platform.Error(w, http.StatusConflict, "PREREQUISITE_CYCLE", "This prerequisite would create a cycle.")
		return
	}
	platform.JSON(w, http.StatusOK, map[string]string{"topic_id": id, "prerequisite_id": input.Prerequisite})
}

// prerequisites lists an active topic's active prerequisites, by name. A link to an archived topic is kept, and
// listed again once that topic is restored.
func (s server) prerequisites(w http.ResponseWriter, r *http.Request) {
	if !topicID(w, r) {
		return
	}
	org := platform.Actor(r).Org
	if _, err := s.store.findTopic(r.Context(), org, r.PathValue("id")); !platform.Found(w, err, "Topic not found.") {
		return
	}
	result, err := s.store.listPrerequisites(r.Context(), org, r.PathValue("id"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// removePrerequisite deletes one prerequisite link, archived topics' included, for a teacher.
func (s server) removePrerequisite(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) || !topicID(w, r) {
		return
	}
	if !platform.ID(r.PathValue("prerequisite")) {
		platform.NotFound(w, "Prerequisite not found.")
		return
	}
	removed, err := s.store.deletePrerequisite(r.Context(), platform.Actor(r).Org, r.PathValue("id"), r.PathValue("prerequisite"))
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !removed {
		platform.NotFound(w, "Prerequisite not found.")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
