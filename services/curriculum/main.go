package main

import (
	_ "embed"
	"learning/platform"
	"log/slog"
	"os"
)

//go:embed migrations/001_init.sql
var migration string

func main() {
	a, err := platform.New("curriculum", migration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("GET /v1/nodes", s.list)
	a.Mux.HandleFunc("GET /v1/nodes/{id}", s.get)
	a.Mux.HandleFunc("POST /v1/nodes", s.create)
	a.Mux.HandleFunc("PATCH /v1/nodes/{id}", s.rename)
	a.Mux.HandleFunc("DELETE /v1/nodes/{id}", s.archive)
	a.Mux.HandleFunc("POST /v1/skills/{id}/prerequisites", s.addPrerequisite)
	if err = a.Run(); err != nil {
		os.Exit(1)
	}
}
