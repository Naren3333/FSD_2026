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
	a, err := platform.New("content", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	store, err := newStorage()
	if err != nil {
		os.Exit(1)
	}
	s := server{a, store}
	a.Checks = append(a.Checks, s.store.check)
	a.Mux.HandleFunc("POST /v1/documents", s.upload)
	a.Mux.HandleFunc("GET /v1/documents", s.list)
	a.Mux.HandleFunc("GET /v1/documents/{id}", s.get)
	a.Mux.HandleFunc("GET /v1/documents/{id}/original", s.original)
	if err = a.Run(a.Outbox); err != nil {
		os.Exit(1)
	}
}
