// Command identity is the Identity & Classroom service: users, classrooms, enrollment, and the classroom
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
	platform.Serve("identity", migrations, "", func(a *platform.App) ([]func(context.Context), error) {
		server{store: pgStore{a.DB}}.routes(a.Mux)
		return nil, nil
	})
}
