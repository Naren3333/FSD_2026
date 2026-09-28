package platform

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/segmentio/kafka-go"
)

const EventMigration = `
CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS inbox (event_id uuid PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());`

type Event struct {
	ID          string          `json:"event_id"`
	Type        string          `json:"event_type"`
	Version     int             `json:"schema_version"`
	Timestamp   time.Time       `json:"timestamp"`
	Correlation string          `json:"correlation_id"`
	Resource    string          `json:"resource_id"`
	Data        json.RawMessage `json:"data"`
}

func Emit(ctx context.Context, tx pgx.Tx, topic, key string, data any) error {
	payload, err := json.Marshal(data)
	if err != nil {
		return err
	}
	e := Event{uuid.NewString(), topic, 1, time.Now().UTC(), Correlation(ctx), key, payload}
	body, err := json.Marshal(e)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "INSERT INTO outbox(id,topic,partition_key,body) VALUES($1,$2,$3,$4)", e.ID, topic, key, body)
	return err
}
func Writer() *kafka.Writer {
	return &kafka.Writer{Addr: kafka.TCP(strings.Split(Required("KAFKA_BROKERS"), ",")...), Balancer: &kafka.Hash{}, RequiredAcks: kafka.RequireAll, MaxAttempts: 3, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
}

func KafkaReady(ctx context.Context) error {
	dialer := &kafka.Dialer{Timeout: 2 * time.Second}
	conn, err := dialer.DialContext(ctx, "tcp", strings.Split(Required("KAFKA_BROKERS"), ",")[0])
	if err != nil {
		return err
	}
	defer conn.Close()
	if err = conn.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return err
	}
	_, err = conn.Brokers()
	return err
}
func (a *App) Outbox(ctx context.Context) {
	writer := Writer()
	defer writer.Close()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		batchCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
		tx, err := a.DB.Begin(batchCtx)
		if err != nil {
			cancel()
			continue
		}
		var id, topic, key string
		var body []byte
		err = tx.QueryRow(batchCtx, `SELECT id,topic,partition_key,body FROM outbox WHERE published_at IS NULL AND attempts<12 AND next_attempt<=now() ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1`).Scan(&id, &topic, &key, &body)
		if errors.Is(err, pgx.ErrNoRows) {
			_ = tx.Rollback(batchCtx)
			cancel()
			continue
		}
		if err != nil {
			_ = tx.Rollback(batchCtx)
			cancel()
			slog.Error("outbox_read_failed")
			continue
		}
		err = writer.WriteMessages(batchCtx, kafka.Message{Topic: topic, Key: []byte(key), Value: body})
		if err == nil {
			_, err = tx.Exec(batchCtx, "UPDATE outbox SET published_at=now() WHERE id=$1", id)
		} else {
			slog.Error("outbox_publish_failed", "event_id", id)
			_, err = tx.Exec(batchCtx, "UPDATE outbox SET attempts=attempts+1,next_attempt=now()+make_interval(secs=>LEAST(300,POWER(2,attempts)::int)) WHERE id=$1", id)
		}
		if err == nil {
			err = tx.Commit(batchCtx)
		} else {
			_ = tx.Rollback(batchCtx)
		}
		if err != nil {
			slog.Error("outbox_commit_failed", "event_id", id)
		}
		cancel()
	}
}

type InvalidEvent struct{ Reason string }

func (e InvalidEvent) Error() string { return e.Reason }
func (a *App) Consume(ctx context.Context, topic string, handler func(context.Context, pgx.Tx, Event) error) {
	reader := kafka.NewReader(kafka.ReaderConfig{Brokers: strings.Split(Required("KAFKA_BROKERS"), ","), Topic: topic, GroupID: a.Name + "-v1", MinBytes: 1, MaxBytes: 2 << 20, CommitInterval: 0})
	defer reader.Close()
	dlq := Writer()
	defer dlq.Close()
	for ctx.Err() == nil {
		msg, err := reader.FetchMessage(ctx)
		if err != nil {
			if ctx.Err() == nil {
				slog.Error("consumer_fetch_failed")
			}
			continue
		}
		var event Event
		err = json.Unmarshal(msg.Value, &event)
		poison := err != nil || !ID(event.ID) || event.Type != topic || event.Version != 1 || !ID(event.Correlation) || event.Timestamp.IsZero() || event.Resource == ""
		completed := false
		for attempt := 0; !poison && attempt < 5 && ctx.Err() == nil; attempt++ {
			workCtx, cancel := context.WithTimeout(context.WithValue(ctx, correlationKey, event.Correlation), 10*time.Second)
			tx, txErr := a.DB.Begin(workCtx)
			if txErr == nil {
				result, insertErr := tx.Exec(workCtx, "INSERT INTO inbox(event_id) VALUES($1) ON CONFLICT DO NOTHING", event.ID)
				txErr = insertErr
				if txErr == nil && result.RowsAffected() > 0 {
					txErr = handler(workCtx, tx, event)
				}
				if txErr == nil {
					txErr = tx.Commit(workCtx)
				} else {
					_ = tx.Rollback(workCtx)
				}
			}
			cancel()
			if txErr == nil {
				completed = true
				break
			}
			var invalid InvalidEvent
			if errors.As(txErr, &invalid) {
				poison = true
				break
			}
			slog.Error("consumer_retry", "event_id", event.ID, "attempt", attempt+1)
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Duration(1<<attempt) * time.Second):
			}
		}
		if poison {
			err = dlq.WriteMessages(ctx, kafka.Message{Topic: topic + ".dlq", Key: msg.Key, Value: msg.Value})
			completed = err == nil
			slog.Error("invalid_event", "event_id", event.ID, "dead_lettered", completed)
		}
		if !completed {
			slog.Error("consumer_stopped_uncommitted", "topic", topic)
			return
		}
		if err = reader.CommitMessages(ctx, msg); err != nil {
			slog.Error("consumer_commit_failed", "event_id", event.ID)
			return
		}
	}
}
