// command identity is the identity & classroom service: users, classrooms, enrollment, and the classroom
// access decisions the other services ask it for.
package main

import (
	"context"
	"embed"
	"learning/platform"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("identity", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		server{store: pgStore{a.DB}}.routes(a.Mux)
		return []func(context.Context){platform.Outbox{DB: a.DB}.Run, signInCleanup{a.DB, newKeycloakUsers()}.run}, nil
	})
}
