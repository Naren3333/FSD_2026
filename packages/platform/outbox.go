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

// Outbox delivery limits. A row is retried with backoff (doubling, at most five minutes apart) for about a day
// before it needs an operator; every minute any backlog is logged as an error until it clears.
const (
	OutboxBatch       = 100
	OutboxMaxAttempts = 300
)

type outboxRow struct {
	id, topic, key string
	body           []byte
}

// Outbox publishes the events services Emit into their outbox table.
type Outbox struct{ DB *pgxpool.Pool }

// Run publishes committed events until ctx ends. Every second it drains due rows in batches of OutboxBatch until
// none are left, so a burst of submissions is delivered in seconds instead of one event per second.
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
		}
	}
}

// publishBatch sends up to OutboxBatch due rows and records, per row, whether Kafka took it. It returns how many
// rows were published; a failed delivery returns 0 so the rest waits for the next tick instead of hammering the
// broker.
func (o Outbox) publishBatch(ctx context.Context, writer *kafka.Writer) (int, error) {
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

// dueRows locks up to OutboxBatch unpublished rows whose next attempt is due, oldest first. A row waits while an
// older event with the same topic and key is backing off after a failed delivery, so one resource's events are not
// published out of order.
func dueRows(ctx context.Context, tx pgx.Tx) ([]outboxRow, error) {
	rows, err := tx.Query(ctx, `SELECT id,topic,partition_key,body FROM outbox o WHERE published_at IS NULL AND attempts<$1 AND next_attempt<=now()
		AND NOT EXISTS(SELECT 1 FROM outbox p WHERE p.topic=o.topic AND p.partition_key=o.partition_key AND p.published_at IS NULL AND p.attempts<$1 AND p.next_attempt>now() AND p.created_at<o.created_at)
		ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT $2`, OutboxMaxAttempts, OutboxBatch)
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

// scheduleRetries counts a failed attempt for each row and backs it off, logging the rows that ran out of attempts.
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

// deliveryFailures maps WriteMessages' error to the messages it concerns: kafka.WriteErrors names each message,
// any other error fails them all.
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

// reportOutbox logs, as an error, events waiting more than five minutes and events that ran out of attempts, so a
// broker outage or a stuck event is visible instead of silent.
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
