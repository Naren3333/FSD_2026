package main

import (
	"context"
	_ "embed"
	"errors"
	"learning/platform"
	"log/slog"
	"os"
	"sync/atomic"
)

//go:embed migrations/001_init.sql
var migration string

func main() {
	a, err := platform.New("grading", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	s := server{a}
	a.Mux.HandleFunc("GET /v1/evaluations", s.list)
	a.Mux.HandleFunc("POST /v1/evaluations/{id}/finalize", s.finalize)
	var consumerRunning atomic.Bool
	a.Checks = append(a.Checks, func(ctx context.Context) error {
		if !consumerRunning.Load() {
			return errors.New("consumer is stopped")
		}
		return platform.KafkaReady(ctx)
	})
	if err = a.Run(a.Outbox, func(ctx context.Context) {
		consumerRunning.Store(true)
		defer consumerRunning.Store(false)
		a.Consume(ctx, "assessment.submitted.v1", consume)
	}); err != nil {
		os.Exit(1)
	}
}
