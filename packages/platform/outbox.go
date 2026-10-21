package platform

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
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

// outboxretention is how long a published event stays in the outbox, the same as kafka keeps the topics
// that carry students' answers and grades (docker-compose.yml).
const OutboxRetention = 7 * 24 * time.Hour

type outboxRow struct {
	id, topic, key string
	body           []byte
}

// outbox publishes the events services emit into their outbox table.
type Outbox struct{ DB *pgxpool.Pool }

// messagewriter is the part of *kafka.writer the outbox uses.
type messageWriter interface {
	WriteMessages(ctx context.Context, msgs ...kafka.Message) error
}

// run publishes committed events until ctx ends.
func (o Outbox) Run(ctx context.Context) {
	writer := Writer()
	defer writer.Close()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var reported time.Time
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		for ctx.Err() == nil {
			sent, err := o.publishBatch(ctx, writer)
			if err != nil {
				slog.Error("outbox_batch_failed", "error_category", fmt.Sprintf("%T", err))
				break
			}
			if sent < OutboxBatch {
				break
			}
		}
		if time.Since(reported) >= time.Minute {
			reported = time.Now()
			o.reportOutbox(ctx)
			o.prune(ctx)
		}
	}
}

// publishbatch sends up to outboxbatch due rows and records, per row, whether kafka took it.
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

// duerows locks up to outboxbatch unpublished rows whose next attempt is due, oldest first.
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

// scheduleretries counts a failed attempt for each row and backs it off, logging the rows that ran out of
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

// deliveryfailures maps writemessages' error to the messages it concerns: kafka.writeerrors names each
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

// reportoutbox logs, as an error, events waiting more than five minutes and events that ran out of
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

// prune deletes published events older than outboxretention, a batch at a time so a large first prune never
// holds a long lock.
func (o Outbox) prune(ctx context.Context) {
	pruneCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if _, err := pruneOutbox(pruneCtx, o.DB, OutboxRetention); err != nil {
		slog.Error("outbox_prune_failed", "error_category", fmt.Sprintf("%T", err))
	}
}

// pruneoutbox deletes up to 1000 rows published more than retention ago and reports how many it deleted.
func pruneOutbox(ctx context.Context, db *pgxpool.Pool, retention time.Duration) (int64, error) {
	tag, err := db.Exec(ctx, "DELETE FROM outbox WHERE id IN (SELECT id FROM outbox WHERE published_at<now()-make_interval(secs=>$1) LIMIT 1000)", retention.Seconds())
	return tag.RowsAffected(), err
}

// remove matching outbox events in the same transaction as the data purge.
func DiscardPendingBy(ctx context.Context, tx pgx.Tx, topics, key, value, org string) error {
	_, err := tx.Exec(ctx, "DELETE FROM outbox WHERE topic LIKE $1 AND body->'data'->>$2::text=$3::text AND body->'data'->>'org_id'=$4", topics, key, value, org)
	return err
}
