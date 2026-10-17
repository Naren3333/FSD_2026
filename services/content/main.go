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
		a.Slow("POST /v1/documents", uploadDeadline)
		server{store: pgStore{a.DB}, access: a.Identity(), files: files, ai: ai, vectors: newVectorCache()}.routes(a.Mux)
		// Two indexing workers: ai-core ingests two PDFs at once.
		x := indexer{a.DB, files, ai, 2 * time.Second}
		// A purge deletes every document of the classroom with its chunks and vectors, and their objects in the
		// bucket: far more than the default 10 seconds for a large classroom.
		classrooms := platform.Consumer{DB: a.DB, Group: "content-classrooms-v1", Timeout: purgeTimeout}
		purge := purger{files}.purge
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run, x.run, x.run,
			func(ctx context.Context) { classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, nil) },
		}, nil
	})
}
