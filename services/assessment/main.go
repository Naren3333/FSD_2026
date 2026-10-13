package main

import (
	"embed"
	"learning/platform"
	"log/slog"
	"os"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	a, err := platform.New("assessment", migrations, platform.EventMigration)
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
