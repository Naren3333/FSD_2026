// Command content is the Content service: classroom documents in object storage, their indexing through
// ai-core, and hybrid search over the indexed chunks.
package main

import (
	"context"
	"embed"
	"learning/platform"
	"time"
)

//go:embed migrations/*.sql
var migrations embed.FS

func main() {
	platform.Serve("content", migrations, platform.EventMigration, func(a *platform.App) ([]func(context.Context), error) {
		files, err := newStorage()
		if err != nil {
			return nil, err
		}
		ai := newAICore()
		a.Checks = append(a.Checks, files.check)
		server{store: pgStore{a.DB}, access: a.Identity(), files: files, ai: ai}.routes(a.Mux)
		// Two indexing workers: ai-core ingests two PDFs at once.
		x := indexer{a.DB, files, ai, 2 * time.Second}
		return []func(context.Context){platform.Outbox{DB: a.DB}.Run, x.run, x.run}, nil
	})
}
