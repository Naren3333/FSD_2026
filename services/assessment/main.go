// Command assessment is the Assessment service: draft and published assessments, student submissions and saved
// work, with submissions published as assessment.submitted events.
package main

import (
	"context"
	"embed"
	"learning/platform"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("assessment", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		s := server{store: pgStore{a.DB}, access: a.Identity(), skills: curriculum{platform.Required("CURRICULUM_URL"), a.Client}}
		s.routes(a.Mux)
		classrooms := platform.Consumer{DB: a.DB, Group: "assessment-classrooms-v1"}
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run,
			func(ctx context.Context) { classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, nil) },
		}, nil
	})
}
