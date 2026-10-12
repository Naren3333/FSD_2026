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
	a, err := platform.New("assessment", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("POST /v1/assessments", s.create)
	a.Mux.HandleFunc("GET /v1/assessments", s.list)
	a.Mux.HandleFunc("GET /v1/assessments/{id}", s.get)
	a.Mux.HandleFunc("POST /v1/assessments/{id}/submissions", s.submit)
	if err = a.Run(a.Outbox); err != nil {
		os.Exit(1)
	}
}
