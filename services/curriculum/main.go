package main

import (
	_ "embed"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
	"strings"
)

//go:embed migrations/001_init.sql
var migration string

type Node struct {
	ID     string  `json:"id"`
	Kind   string  `json:"kind"`
	Name   string  `json:"name"`
	Parent *string `json:"parent_id"`
}
type Input struct {
	Kind   string  `json:"kind"`
	Name   string  `json:"name"`
	Parent *string `json:"parent_id"`
}

func valid(v Input) bool {
	switch v.Kind {
	case "subject", "course", "unit", "topic", "skill", "objective":
	default:
		return false
	}
	return strings.TrimSpace(v.Name) != "" && len(v.Name) <= 160 && (v.Parent == nil || platform.ID(*v.Parent))
}
func main() {
	a, err := platform.New("curriculum", migration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	a.Mux.HandleFunc("GET /v1/nodes", func(w http.ResponseWriter, r *http.Request) {
		rows, err := a.DB.Query(r.Context(), "SELECT id,kind,name,parent_id FROM nodes WHERE org_id=$1 AND NOT archived ORDER BY name,id LIMIT 200", platform.Actor(r).Org)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer rows.Close()
		result := []Node{}
		for rows.Next() {
			var v Node
			if err = rows.Scan(&v.ID, &v.Kind, &v.Name, &v.Parent); err != nil {
				platform.Fail(w, err)
				return
			}
			result = append(result, v)
		}
		if rows.Err() != nil {
			platform.Fail(w, rows.Err())
			return
		}
		platform.JSON(w, 200, result)
	})
	a.Mux.HandleFunc("GET /v1/nodes/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !platform.ID(r.PathValue("id")) {
			platform.Error(w, 404, "NOT_FOUND", "Node not found.")
			return
		}
		var v Node
		err := a.DB.QueryRow(r.Context(), "SELECT id,kind,name,parent_id FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", r.PathValue("id"), platform.Actor(r).Org).Scan(&v.ID, &v.Kind, &v.Name, &v.Parent)
		if err == pgx.ErrNoRows {
			platform.Error(w, 404, "NOT_FOUND", "Node not found.")
			return
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		platform.JSON(w, 200, v)
	})
	a.Mux.HandleFunc("POST /v1/nodes", func(w http.ResponseWriter, r *http.Request) {
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
			var kind string
			err := a.DB.QueryRow(r.Context(), "SELECT kind FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", *v.Parent, org).Scan(&kind)
			expected := map[string]string{"course": "subject", "unit": "course", "topic": "unit", "skill": "topic", "objective": "skill"}
			if err != nil || expected[v.Kind] != kind {
				platform.Error(w, 422, "INVALID_PARENT", "Parent must be the preceding curriculum level in your organization.")
				return
			}
		}
		id := uuid.NewString()
		_, err := a.DB.Exec(r.Context(), "INSERT INTO nodes(id,org_id,kind,name,parent_id) VALUES($1,$2,$3,$4,$5)", id, org, v.Kind, strings.TrimSpace(v.Name), v.Parent)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		platform.JSON(w, 201, Node{id, v.Kind, v.Name, v.Parent})
	})
	a.Mux.HandleFunc("PATCH /v1/nodes/{id}", func(w http.ResponseWriter, r *http.Request) {
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
		res, err := a.DB.Exec(r.Context(), "UPDATE nodes SET name=$1 WHERE id=$2 AND org_id=$3 AND NOT archived", strings.TrimSpace(v.Name), r.PathValue("id"), platform.Actor(r).Org)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if res.RowsAffected() == 0 {
			platform.Error(w, 404, "NOT_FOUND", "Node not found.")
			return
		}
		platform.JSON(w, 200, map[string]string{"id": r.PathValue("id"), "name": v.Name})
	})
	a.Mux.HandleFunc("DELETE /v1/nodes/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !platform.Teacher(w, r) {
			return
		}
		if !platform.ID(r.PathValue("id")) {
			platform.Error(w, 404, "NOT_FOUND", "Node not found.")
			return
		}
		res, err := a.DB.Exec(r.Context(), "UPDATE nodes SET archived=true WHERE id=$1 AND org_id=$2 AND NOT archived AND NOT EXISTS(SELECT 1 FROM nodes child WHERE child.parent_id=$1 AND NOT child.archived)", r.PathValue("id"), platform.Actor(r).Org)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if res.RowsAffected() == 0 {
			platform.Error(w, 409, "ARCHIVE_CONFLICT", "Node is absent, already archived, or has active children.")
			return
		}
		w.WriteHeader(204)
	})
	a.Mux.HandleFunc("POST /v1/skills/{id}/prerequisites", func(w http.ResponseWriter, r *http.Request) {
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
		tx, err := a.DB.Begin(r.Context())
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer tx.Rollback(r.Context())
		if _, err = tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock(271828)"); err != nil {
			platform.Fail(w, err)
			return
		}
		var count int
		err = tx.QueryRow(r.Context(), "SELECT count(*) FROM nodes WHERE id=ANY($1::uuid[]) AND org_id=$2 AND kind='skill' AND NOT archived", []string{id, input.Prerequisite}, platform.Actor(r).Org).Scan(&count)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if count != 2 {
			platform.Error(w, 422, "INVALID_PREREQUISITE", "Both skills must be active in your organization.")
			return
		}
		var cycle bool
		err = tx.QueryRow(r.Context(), `WITH RECURSIVE dependencies(id) AS (SELECT prerequisite_id FROM prerequisites WHERE skill_id=$1 UNION SELECT p.prerequisite_id FROM prerequisites p JOIN dependencies d ON p.skill_id=d.id) SELECT EXISTS(SELECT 1 FROM dependencies WHERE id=$2)`, input.Prerequisite, id).Scan(&cycle)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if cycle {
			platform.Error(w, 409, "PREREQUISITE_CYCLE", "This prerequisite would create a cycle.")
			return
		}
		_, err = tx.Exec(r.Context(), "INSERT INTO prerequisites(skill_id,prerequisite_id) VALUES($1,$2) ON CONFLICT DO NOTHING", id, input.Prerequisite)
		if err == nil {
			err = tx.Commit(r.Context())
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		platform.JSON(w, 200, map[string]string{"skill_id": id, "prerequisite_id": input.Prerequisite})
	})
	if err = a.Run(); err != nil {
		os.Exit(1)
	}
}
