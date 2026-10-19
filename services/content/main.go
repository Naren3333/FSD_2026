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
		a.MetricGauges = func(ctx context.Context) (map[string]float64, error) {
			var index, cleanup, failed float64
			var age float64
			err := a.DB.QueryRow(ctx, `SELECT (SELECT count(*) FROM index_jobs),(SELECT count(*) FROM object_cleanup_jobs),
			(SELECT count(*) FROM object_cleanup_jobs WHERE attempts>0),
			coalesce((SELECT extract(epoch FROM now()-min(created_at)) FROM index_jobs),0)`).Scan(&index, &cleanup, &failed, &age)
			return map[string]float64{"learning_content_index_jobs": index, "learning_content_cleanup_jobs": cleanup, "learning_content_cleanup_retried_jobs": failed, "learning_content_oldest_index_seconds": age}, err
		}
		a.Slow("POST /v1/documents", uploadDeadline)
		server{store: pgStore{a.DB}, access: a.Identity(), files: files, ai: ai, vectors: newVectorCache()}.routes(a.Mux)
		// Two indexing workers: ai-core ingests two PDFs at once.
		x := indexer{a.DB, files, ai, 2 * time.Second}
		// A purge deletes every document of the classroom with its chunks and vectors, and their objects in the
		// bucket: far more than the default 10 seconds for a large classroom.
		classrooms := platform.Consumer{DB: a.DB, Group: "content-classrooms-v1", Timeout: purgeTimeout}
		purge := purger{}.purge
		return []func(context.Context){
			platform.Outbox{DB: a.DB}.Run, x.run, x.run, sweeper{a.DB, files}.run, cleaner{a.DB, files}.run,
			func(ctx context.Context) { classrooms.Consume(ctx, platform.TopicClassroomDeleted, purge, nil) },
		}, nil
	})
}
