// command curriculum is the curriculum service: an organization's topics and their prerequisites.
package main

import (
	"context"
	"embed"
	"learning/platform"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("curriculum", migrations, "", func(a *platform.App) ([]func(context.Context), error) {
		server{store: pgStore{a.DB}}.routes(a.Mux)
		return nil, nil
	})
}
