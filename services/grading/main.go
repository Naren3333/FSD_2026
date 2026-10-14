// Command grading is the Grading service: draft scores computed from submissions, which a classroom's teacher
// reviews and finalizes.
package main

import (
	"context"
	"embed"
	"errors"
	"learning/platform"
	"sync/atomic"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("grading", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		server{store: pgStore{a.DB}, access: a.Identity()}.routes(a.Mux)
		var consumerRunning atomic.Bool
		a.Checks = append(a.Checks, func(ctx context.Context) error {
			if !consumerRunning.Load() {
				return errors.New("consumer is stopped")
			}
			return platform.KafkaReady(ctx)
		})
		submissions := platform.Consumer{DB: a.DB, Group: "grading-v1"}
		classrooms := platform.Consumer{DB: a.DB, Group: "grading-classrooms-v1"}
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run,
			func(ctx context.Context) {
				submissions.Consume(ctx, "assessment.submitted.v1", consume, &consumerRunning)
			},
			func(ctx context.Context) { classrooms.Consume(ctx, "identity.classroom.deleted.v1", purge, nil) },
		}, nil
	})
}
