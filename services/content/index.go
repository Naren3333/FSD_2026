package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"learning/platform"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

const (
	dimensions    = 1536 // document_chunks.embedding is vector(1536)
	maxAttempts   = 6    // ai-core outages are retried this many times before the document fails
	leaseTime     = 30 * time.Minute
	ingestTimeout = 20 * time.Minute // shorter than the lease, so a live job is never taken over
	maxDetail     = 2000             // characters of a failure or retry reason kept
)

var figureName = regexp.MustCompile(`^[a-f0-9]{16}\.png$`)
var documentFailures = map[string]bool{"NO_TEXT_LAYER": true, "ENCRYPTED": true, "CORRUPT": true, "TOO_LARGE": true}

// objects is the part of object storage the indexer uses.
type objects interface {
	open(ctx context.Context, key string) (io.ReadCloser, int64, error)
	put(ctx context.Context, key string, data []byte, contentType string) error
	removeAll(ctx context.Context, prefix string) error
}

// indexer sends PDFs with a pending index_jobs row to ai-core and stores the chunks, vectors and figure crops it
// returns. Every write that ends a job checks the job's lease, so a run that lost its lease (it outlived it, or
// another worker took the job over) changes nothing.
type indexer struct {
	db    *pgxpool.Pool
	store objects
	ai    ingester
	poll  time.Duration
}

type job struct {
	document, lease, org, classroom, title, key, docType, owner, mime string
	revision, attempts                                                int
}

func (x indexer) run(ctx context.Context) {
	ticker := time.NewTicker(x.poll)
	defer ticker.Stop()
	for {
		for ctx.Err() == nil {
			found, err := x.next(ctx)
			if err != nil {
				slog.Error("index_job_failed", "error_category", fmt.Sprintf("%T", err))
			}
			if !found || err != nil {
				break
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// next runs the next due job, if any.
func (x indexer) next(ctx context.Context) (bool, error) {
	j, err := x.claim(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, x.process(ctx, j)
}

func (x indexer) claim(ctx context.Context) (job, error) {
	j := job{lease: uuid.NewString()}
	err := x.db.QueryRow(ctx, `WITH due AS (SELECT document_id FROM index_jobs WHERE next_attempt<=now() ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED)
UPDATE index_jobs j SET attempts=j.attempts+1, lease=$1, next_attempt=now()+make_interval(secs=>$2)
FROM due, documents d WHERE j.document_id=due.document_id AND d.id=j.document_id
RETURNING j.document_id, j.attempts, d.org_id, d.classroom_id, d.title, d.object_key, d.doc_type, d.revision, d.owner_id, d.mime_type`, j.lease, leaseTime.Seconds()).
		Scan(&j.document, &j.attempts, &j.org, &j.classroom, &j.title, &j.key, &j.docType, &j.revision, &j.owner, &j.mime)
	return j, err
}

func (x indexer) process(ctx context.Context, j job) error {
	result, err := x.ingest(ctx, j)
	// Bookkeeping must land even when shutdown cancels ctx mid-ingest.
	book, cancel := context.WithTimeout(context.WithoutCancel(ctx), 30*time.Second)
	defer cancel()
	if ctx.Err() != nil {
		return x.release(book, j)
	}
	if err != nil {
		if limit, ok := spendLimit(err); ok {
			return x.wait(book, j, limit.ResetsAt, "the uploading teacher's daily AI allowance is used up")
		}
		if !transient(err) {
			return x.fail(book, j, "INTERNAL", err.Error())
		}
		return x.retry(book, j, err.Error())
	}
	if result.Outcome != statusIndexed {
		reason, detail := "INTERNAL", "ai-core returned outcome "+result.Outcome
		if result.FailureReason != nil && documentFailures[*result.FailureReason] {
			reason = *result.FailureReason
		}
		if result.FailureDetail != nil {
			detail = *result.FailureDetail
		}
		return x.fail(book, j, reason, detail)
	}
	if err = validate(result); err != nil {
		return x.fail(book, j, "INTERNAL", err.Error())
	}
	for _, f := range result.Figures {
		// Crops are named by content hash, so a retried or repeated run rewrites identical objects.
		if err = x.store.put(ctx, figureKey(j.org, j.document, f.Image), f.PNG, mimePNG); err != nil {
			return x.retry(book, j, "figure storage unavailable")
		}
	}
	if err = x.complete(book, j, result); err != nil {
		return x.retry(book, j, "saving chunks failed")
	}
	return nil
}

func (x indexer) ingest(ctx context.Context, j job) (ingestResult, error) {
	ctx, cancel := context.WithTimeout(ctx, ingestTimeout)
	defer cancel()
	object, size, err := x.store.open(ctx, j.key)
	if err != nil {
		return ingestResult{}, fmt.Errorf("original unavailable: %w", err)
	}
	defer object.Close()
	// The teacher who uploaded the document pays for its model calls.
	return x.ai.ingest(ctx, j.title, j.mime, object, size, billed{UserID: j.owner, OrgID: j.org, Role: "teacher"})
}

// validate rejects an ingest result Content cannot store as-is, rather than storing part of it.
func validate(r ingestResult) error {
	if r.PageCount == nil || r.EmbeddingModel == nil || *r.EmbeddingModel == "" || len(r.Chunks) == 0 {
		return errors.New("indexed result without pages, model or chunks")
	}
	figures := map[string]bool{}
	for _, f := range r.Figures {
		if !figureName.MatchString(f.Image) || len(f.PNG) == 0 {
			return errors.New("invalid figure")
		}
		figures[f.Image] = true
	}
	for i, c := range r.Chunks {
		if c.Ordinal != i || len(c.Embedding) != dimensions || strings.TrimSpace(c.Content) == "" {
			return fmt.Errorf("chunk %d: bad ordinal, empty content or %d dimensions", i, len(c.Embedding))
		}
		if c.Image != nil && !figures[*c.Image] {
			return fmt.Errorf("chunk %d points at a missing figure", i)
		}
	}
	return nil
}

func figureKey(org, document, image string) string { return org + "/" + document + "/figures/" + image }

var errLostLease = errors.New("the job's lease was lost")

// complete replaces the document's chunks, marks it indexed, ends the job and emits content.indexed.v1 in one
// transaction. When the run lost its lease because the document was purged meanwhile (its classroom was deleted),
// the figure crops this run uploaded are removed too, since the purge may have run before they were stored.
func (x indexer) complete(ctx context.Context, j job, r ingestResult) error {
	err := x.finish(ctx, j, func(tx pgx.Tx) error { return storeResult(ctx, tx, j, r) })
	if !errors.Is(err, errLostLease) {
		return err
	}
	if len(r.Figures) == 0 {
		return nil
	}
	var exists bool
	if err = x.db.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM documents WHERE id=$1)", j.document).Scan(&exists); err != nil || exists {
		return err // a run that took the job over stores the same crops, by content hash
	}
	// The deletion's worker may already have cleaned this prefix before these late crops were written.
	// Record fresh durable cleanup before the best-effort immediate removal.
	if _, err = x.db.Exec(ctx, "INSERT INTO object_cleanup_jobs(prefix) VALUES($1) ON CONFLICT DO NOTHING", j.org+"/"+j.document+"/"); err != nil {
		return err
	}
	return x.store.removeAll(ctx, j.org+"/"+j.document+"/")
}

// finish deletes the job and lets write record the outcome, in one transaction. When another run took the job over
// or the document is gone, nothing is changed and it returns errLostLease.
func (x indexer) finish(ctx context.Context, j job, write func(pgx.Tx) error) error {
	tx, err := x.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = endJob(ctx, tx, j); err != nil {
		if errors.Is(err, errLostLease) {
			slog.Info("index_result_discarded", "document_id", j.document)
		}
		return err
	}
	if err = write(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

// storeResult replaces the document's chunks with the new ones, marks the document indexed and emits
// content.indexed.v1.
func storeResult(ctx context.Context, tx pgx.Tx, j job, r ingestResult) error {
	if _, err := tx.Exec(ctx, "DELETE FROM document_chunks WHERE document_id=$1", j.document); err != nil {
		return err
	}
	batch := &pgx.Batch{}
	for _, c := range r.Chunks {
		cites := c.Cites
		if cites == nil {
			cites = []string{}
		}
		batch.Queue(`INSERT INTO document_chunks(document_id,org_id,classroom_id,doc_type,ordinal,section_path,page_start,page_end,content,embed_text,token_count,kind,source,image,label,cites,embedding_model,embedding)
VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,CAST($18::text AS vector))`,
			j.document, j.org, j.classroom, j.docType, c.Ordinal, c.SectionPath, c.PageStart, c.PageEnd, c.Content, c.EmbedText, c.TokenCount, c.Kind, c.Source, c.Image, c.Label, cites, *r.EmbeddingModel, vectorText(c.Embedding))
	}
	results := tx.SendBatch(ctx, batch)
	for range r.Chunks {
		if _, err := results.Exec(); err != nil {
			results.Close()
			return err
		}
	}
	if err := results.Close(); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, "UPDATE documents SET status='indexed',page_count=$2,chunk_count=$3,embedding_model=$4,indexed_at=now(),failure_reason=NULL,failure_detail=NULL WHERE id=$1", j.document, *r.PageCount, len(r.Chunks), *r.EmbeddingModel); err != nil {
		return err
	}
	return platform.Emit(ctx, tx, "content.indexed.v1", j.document, map[string]any{"document_id": j.document, "classroom_id": j.classroom, "org_id": j.org, "revision": j.revision, "doc_type": j.docType, "page_count": *r.PageCount, "chunk_count": len(r.Chunks), "embedding_model": *r.EmbeddingModel})
}

// fail marks the document failed with its reason and ends the job. Chunks from an earlier index stay searchable,
// with the page and chunk counts, model and time of that index, until a later reindex succeeds.
func (x indexer) fail(ctx context.Context, j job, reason, detail string) error {
	slog.Warn("index_failed", "document_id", j.document, "reason", reason, "attempts", j.attempts)
	err := x.finish(ctx, j, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, "UPDATE documents SET status='failed',failure_reason=$2,failure_detail=$3 WHERE id=$1", j.document, reason, truncate(detail, maxDetail))
		return err
	})
	if errors.Is(err, errLostLease) {
		return nil
	}
	return err
}

// retry gives the job back with a backoff, or fails the document once the attempts are spent.
func (x indexer) retry(ctx context.Context, j job, reason string) error {
	if j.attempts >= maxAttempts {
		return x.fail(ctx, j, "AI_UNAVAILABLE", fmt.Sprintf("gave up after %d attempts: %s", j.attempts, reason))
	}
	slog.Warn("index_retry", "document_id", j.document, "attempts", j.attempts, "reason", reason)
	_, err := x.db.Exec(ctx, "UPDATE index_jobs SET lease=NULL,next_attempt=now()+make_interval(secs=>$3),last_error=$4 WHERE document_id=$1 AND lease=$2", j.document, j.lease, backoff(j.attempts).Seconds(), truncate(reason, maxDetail))
	return err
}

// wait puts the job off until `until` (when the uploader's daily AI allowance resets) without counting the
// attempt: being over the allowance is not a failure of the document or of ai-core.
func (x indexer) wait(ctx context.Context, j job, until time.Time, reason string) error {
	if until.Before(time.Now()) {
		until = time.Now().Add(time.Hour)
	}
	slog.Info("index_waiting_for_allowance", "document_id", j.document, "until", until)
	_, err := x.db.Exec(ctx, "UPDATE index_jobs SET lease=NULL,next_attempt=$3,attempts=attempts-1,last_error=$4 WHERE document_id=$1 AND lease=$2", j.document, j.lease, until, truncate(reason, maxDetail))
	return err
}

// release gives back a job interrupted by shutdown without counting the attempt.
func (x indexer) release(ctx context.Context, j job) error {
	_, err := x.db.Exec(ctx, "UPDATE index_jobs SET lease=NULL,next_attempt=now(),attempts=attempts-1 WHERE document_id=$1 AND lease=$2", j.document, j.lease)
	return err
}

func endJob(ctx context.Context, tx pgx.Tx, j job) error {
	tag, err := tx.Exec(ctx, "DELETE FROM index_jobs WHERE document_id=$1 AND lease=$2", j.document, j.lease)
	if err == nil && tag.RowsAffected() == 0 {
		err = errLostLease
	}
	return err
}

// backoff after the nth attempt: 30 s, 1, 2, 4, 8 minutes, at most 30.
func backoff(attempts int) time.Duration {
	d := 30 * time.Second << max(0, min(attempts-1, 6))
	return min(d, 30*time.Minute)
}

func vectorText(v []float32) string {
	var b strings.Builder
	b.WriteByte('[')
	for i, f := range v {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(strconv.FormatFloat(float64(f), 'g', -1, 32))
	}
	b.WriteByte(']')
	return b.String()
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
