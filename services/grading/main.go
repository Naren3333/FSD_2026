// Command grading is the Grading service: draft scores computed from submissions, with AI-suggested marks for the
// questions that need the teacher when an assessment asks for them, which a classroom's teacher marks, finalizes
// and releases to students.
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
		// Ready only while all four consumers run: a stalled one would otherwise go unnoticed.
		var v1Running, v2Running, v3Running, classroomsRunning atomic.Bool
		a.Checks = append(a.Checks, func(ctx context.Context) error {
			if !v1Running.Load() || !v2Running.Load() || !v3Running.Load() || !classroomsRunning.Load() {
				return errors.New("a consumer is stopped")
			}
			return platform.KafkaReady(ctx)
		})
		// Older submission versions keep their consumer groups, so events published before v3 are still graded once.
		submissionsV1 := platform.Consumer{DB: a.DB, Group: "grading-v1"}
		submissionsV2 := platform.Consumer{DB: a.DB, Group: "grading-submitted-v2"}
		submissions := platform.Consumer{DB: a.DB, Group: "grading-submitted-v3"}
		classrooms := platform.Consumer{DB: a.DB, Group: "grading-classrooms-v1"}
		marking := marker{a.DB, aiSuggester{platform.NewAICore()}}
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run,
			func(ctx context.Context) { submissionsV1.Consume(ctx, topicSubmittedV1, consume, &v1Running) },
			func(ctx context.Context) { submissionsV2.Consume(ctx, topicSubmittedV2, consume, &v2Running) },
			func(ctx context.Context) { submissions.Consume(ctx, topicSubmittedV3, consume, &v3Running) },
			marking.Run,
			marking.Run,
			func(ctx context.Context) {
				classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, &classroomsRunning)
			},
		}, nil
	})
}
