package platform

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

// outbox delivery limits.
const (
	OutboxBatch       = 100
	OutboxMaxAttempts = 300
)

// OutboxRetention is how long a published event stays in the outbox, the same as kafka keeps the topics
// that carry students' answers and grades (docker-compose.yml).
const OutboxRetention = 7 * 24 * time.Hour

type outboxRow struct {
	id, topic, key string
	body           []byte
}

// outbox publishes the events services emit into their outbox table.
type Outbox struct{ DB *pgxpool.Pool }

// messageWriter is the part of *kafka.Writer the outbox uses.
type messageWriter interface {
	WriteMessages(ctx context.Context, msgs ...kafka.Message) error
}

// run publishes committed events until ctx ends.
func (o Outbox) Run(ctx context.Context) {
	writer := Writer()
	defer writer.Close()
	o.run(ctx, writer)
}

// run publishes through writer, and housekeeps beside it, until ctx ends and housekeeping has stopped.
func (o Outbox) run(ctx context.Context, writer messageWriter) {
	var housekeeping sync.WaitGroup
	housekeeping.Go(func() { o.housekeep(ctx) })
	defer housekeeping.Wait()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		o.drain(ctx, writer, time.Now().Add(time.Second))
	}
}

// housekeep reports and prunes the outbox every minute until ctx ends, apart from publishing, so a long
// prune never holds events back.
func (o Outbox) housekeep(ctx context.Context) {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		o.reportOutbox(ctx)
		o.prune(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// drain publishes batches until none is due or the deadline passes. a batch holds only the oldest event of each key, so a key's later events go out in the following batches,
// not a tick later each.
func (o Outbox) drain(ctx context.Context, writer messageWriter, deadline time.Time) {
	for ctx.Err() == nil {
		sent, err := o.publishBatch(ctx, writer)
		if err != nil {
			slog.Error("outbox_batch_failed", "error_category", fmt.Sprintf("%T", err))
			return
		}
		if sent == 0 || !time.Now().Before(deadline) {
			return
		}
	}
}

// publishBatch sends up to OutboxBatch due rows and records, per row, whether kafka took it.
func (o Outbox) publishBatch(ctx context.Context, writer messageWriter) (int, error) {
	batchCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	tx, err := o.DB.Begin(batchCtx)
	if err != nil {
		return 0, err
	}
	defer tx.Rollback(batchCtx)
	batch, err := dueRows(batchCtx, tx)
	if err != nil || len(batch) == 0 {
		return 0, err
	}
	messages := make([]kafka.Message, len(batch))
	for i, row := range batch {
		messages[i] = kafka.Message{Topic: row.topic, Key: []byte(row.key), Value: row.body}
	}
	failed := deliveryFailures(writer.WriteMessages(batchCtx, messages...), len(batch))
	published, retry := []string{}, []string{}
	for i, row := range batch {
		if failed[i] {
			retry = append(retry, row.id)
		} else {
			published = append(published, row.id)
		}
	}
	if err = markPublished(batchCtx, tx, published); err != nil {
		return 0, err
	}
	if err = scheduleRetries(batchCtx, tx, retry); err != nil {
		return 0, err
	}
	if err = tx.Commit(batchCtx); err != nil {
		return 0, err
	}
	if len(retry) > 0 {
		return 0, nil
	}
	return len(batch), nil
}

// dueRows locks up to OutboxBatch unpublished rows whose next attempt is due, oldest first.
func dueRows(ctx context.Context, tx pgx.Tx) ([]outboxRow, error) {
	rows, err := tx.Query(ctx, `SELECT id,topic,partition_key,body FROM outbox o WHERE published_at IS NULL AND attempts<$1 AND next_attempt<=now()
		AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.topic=o.topic AND p.partition_key=o.partition_key AND p.published_at IS NULL AND p.attempts<$1 AND (p.created_at,p.id)<(o.created_at,o.id))
		ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT $2`, OutboxMaxAttempts, OutboxBatch)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	batch := []outboxRow{}
	for rows.Next() {
		var row outboxRow
		if err = rows.Scan(&row.id, &row.topic, &row.key, &row.body); err != nil {
			return nil, err
		}
		batch = append(batch, row)
	}
	return batch, rows.Err()
}

func markPublished(ctx context.Context, tx pgx.Tx, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	_, err := tx.Exec(ctx, "UPDATE outbox SET published_at=now() WHERE id=ANY($1::uuid[])", ids)
	return err
}

// scheduleRetries counts a failed attempt for each row and backs it off, logging the rows that ran out of
// attempts.
func scheduleRetries(ctx context.Context, tx pgx.Tx, ids []string) error {
	if len(ids) == 0 {
		return nil
	}
	slog.Error("outbox_publish_failed", "events", len(ids))
	var abandoned []string
	err := tx.QueryRow(ctx, `WITH failed AS (UPDATE outbox SET attempts=attempts+1,next_attempt=now()+make_interval(secs=>LEAST(300,POWER(2,LEAST(attempts,9))::int)) WHERE id=ANY($1::uuid[]) RETURNING id,attempts)
		SELECT coalesce(array_agg(id::text),'{}') FROM failed WHERE attempts>=$2`, ids, OutboxMaxAttempts).Scan(&abandoned)
	if err != nil {
		return err
	}
	for _, id := range abandoned {
		slog.Error("outbox_event_abandoned", "event_id", id, "attempts", OutboxMaxAttempts)
	}
	return nil
}

// deliveryFailures maps WriteMessages' error to the messages it concerns: kafka.WriteErrors names each
// message, any other error fails them all.
func deliveryFailures(err error, n int) []bool {
	failed := make([]bool, n)
	if err == nil {
		return failed
	}
	var each kafka.WriteErrors
	if errors.As(err, &each) && len(each) == n {
		for i, e := range each {
			failed[i] = e != nil
		}
		return failed
	}
	for i := range failed {
		failed[i] = true
	}
	return failed
}

// reportOutbox logs, as an error, events waiting more than five minutes and events that ran out of
// attempts, so a broker outage or a stuck event is visible instead of silent.
func (o Outbox) reportOutbox(ctx context.Context) {
	reportCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	var waiting, abandoned int
	err := o.DB.QueryRow(reportCtx, `SELECT count(*) FILTER (WHERE attempts<$1 AND created_at<now()-interval '5 minutes'), count(*) FILTER (WHERE attempts>=$1) FROM outbox WHERE published_at IS NULL`, OutboxMaxAttempts).Scan(&waiting, &abandoned)
	if err != nil {
		slog.Error("outbox_report_failed")
		return
	}
	if waiting > 0 || abandoned > 0 {
		slog.Error("outbox_backlog", "waiting_over_5_minutes", waiting, "abandoned", abandoned)
	}
}

// pruneBatch is how many published events one prune statement deletes.
const pruneBatch = 1000

// prune deletes published events older than OutboxRetention, a batch at a time so no statement holds a long
// lock, until none is left or ten seconds have passed.
func (o Outbox) prune(ctx context.Context) {
	pruneCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	pruned, err := pruneAll(pruneCtx, o.DB, OutboxRetention)
	switch {
	case err != nil && ctx.Err() == nil && errors.Is(pruneCtx.Err(), context.DeadlineExceeded):
		// a large backlog takes more than one window; the next minute carries on.
		slog.Info("outbox_prune_continues", "pruned", pruned)
	case err != nil && ctx.Err() == nil:
		slog.Error("outbox_prune_failed", "error_category", fmt.Sprintf("%T", err))
	}
}

// pruneAll runs pruneOutbox until a batch comes back short, and reports how many it deleted in all.
func pruneAll(ctx context.Context, db *pgxpool.Pool, retention time.Duration) (int64, error) {
	var total int64
	for {
		n, err := pruneOutbox(ctx, db, retention)
		total += n
		if err != nil || n < pruneBatch {
			return total, err
		}
	}
}

// pruneSQL deletes up to $2 events published more than $1 seconds ago, found through outbox_published.
const pruneSQL = "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE published_at<now()-make_interval(secs=>$1) LIMIT $2)"

// pruneOutbox deletes up to pruneBatch rows published more than retention ago and reports how many it
// deleted.
func pruneOutbox(ctx context.Context, db *pgxpool.Pool, retention time.Duration) (int64, error) {
	tag, err := db.Exec(ctx, pruneSQL, retention.Seconds(), pruneBatch)
	return tag.RowsAffected(), err
}

// OutboxIndexes are the outbox's own indexes, for every service with an outbox to build (IndexBuilder): the
// one its prune finds published events through, so each batch reads only the events it deletes. its prunes
// scan the outbox until it is built.
func OutboxIndexes() []Index {
	return []Index{{Name: "outbox_published", Table: "outbox", Definition: "(published_at) WHERE published_at IS NOT NULL"}}
}

// PurgeKey is a data field DiscardPendingBy matches outbox events by, with the partial expression index over
// the events that carry it (PurgeIndexes), so a purge reads only its own events instead of every body the
// outbox keeps. only the keys below exist, so a purge by another field does not compile.
type PurgeKey struct{ field, index string }

// the fields outbox events are discarded by.
var (
	PurgeByAssessment = PurgeKey{"assessment_id", "outbox_data_assessment"}
	PurgeByClassroom  = PurgeKey{"classroom_id", "outbox_data_classroom"}
	PurgeByStudent    = PurgeKey{"student_id", "outbox_data_student"}
)

// expression is what a purge by the key matches and its index covers.
func (k PurgeKey) expression() string { return "(body->'data'->>'" + k.field + "')" }

// discardSQL is DiscardPendingBy's statement for the key.
func discardSQL(key PurgeKey) string {
	return "DELETE FROM outbox WHERE " + key.expression() + "=$2 AND topic LIKE $1 AND body->'data'->>'org_id'=$3"
}

// remove matching outbox events, published or not, in the same transaction as the data purge.
func DiscardPendingBy(ctx context.Context, tx pgx.Tx, topics string, key PurgeKey, value, org string) error {
	if key.field == "" {
		return errors.New("outbox events are discarded by one of the purge keys")
	}
	_, err := tx.Exec(ctx, discardSQL(key), topics, value, org)
	return err
}

// PurgeIndexes are the outbox indexes for the keys a service discards its events by (DiscardPendingBy), for it
// to build (IndexBuilder); its purges scan the outbox until they are built.
func PurgeIndexes(keys ...PurgeKey) []Index {
	indexes := make([]Index, 0, len(keys))
	for _, key := range keys {
		if key.field == "" {
			panic("outbox indexes are built for the purge keys")
		}
		field := key.expression()
		indexes = append(indexes, Index{Name: key.index, Table: "outbox", Definition: "(" + field + ") WHERE " + field + " IS NOT NULL"})
	}
	return indexes
}
