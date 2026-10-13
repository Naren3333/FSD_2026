package main

import (
	"embed"
	"learning/platform"
	"log/slog"
	"os"
	"time"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	a, err := platform.New("content", migrations, platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	store, err := newStorage()
	if err != nil {
		os.Exit(1)
	}
	s := server{a, store, newAICore()}
	a.Checks = append(a.Checks, s.store.check)
	a.Mux.HandleFunc("POST /v1/documents", s.upload)
	a.Mux.HandleFunc("GET /v1/documents", s.list)
	a.Mux.HandleFunc("GET /v1/documents/{id}", s.get)
	a.Mux.HandleFunc("GET /v1/documents/{id}/original", s.original)
	a.Mux.HandleFunc("GET /v1/documents/{id}/figures/{image}", s.figure)
	a.Mux.HandleFunc("POST /v1/documents/{id}/reindex", s.reindex)
	a.Mux.HandleFunc("POST /v1/search", s.search)
	// Two indexing workers: ai-core ingests two PDFs at once.
	x := indexer{a.DB, store, s.ai, 2 * time.Second}
	if err = a.Run(a.Outbox, x.run, x.run); err != nil {
		os.Exit(1)
	}
}
