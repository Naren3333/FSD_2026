package platform

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/segmentio/kafka-go"
)

// Consumer reads a topic into a service's database, recording each event in the inbox table.
type Consumer struct {
	DB    *pgxpool.Pool
	Group string // the Kafka consumer group, one per service
}

// Consume processes a topic for as long as ctx lives. A session stops without committing when an event keeps
// failing (the database is down, say); Consume then waits, backing off to a minute, and starts a new session from
// the last committed offset, so the event is retried rather than skipped and the consumer never stays down.
// healthy, if given, is true while a session runs and false while it waits to restart.
func (c Consumer) Consume(ctx context.Context, topic string, handler Handler, healthy *atomic.Bool) {
	backoff := 5 * time.Second
	for ctx.Err() == nil {
		if healthy != nil {
			healthy.Store(true)
		}
		progressed := c.consumeSession(ctx, topic, handler)
		if healthy != nil {
			healthy.Store(false)
		}
		if ctx.Err() != nil {
			return
		}
		if progressed {
			backoff = 5 * time.Second
		}
		slog.Error("consumer_restarting", "topic", topic, "after_seconds", int(backoff.Seconds()))
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		backoff = min(2*backoff, time.Minute)
	}
}

// consumeSession reads until ctx ends or an event cannot be completed, and reports whether it committed anything.
func (c Consumer) consumeSession(ctx context.Context, topic string, handler Handler) bool {
	reader := kafka.NewReader(kafka.ReaderConfig{Brokers: strings.Split(Required("KAFKA_BROKERS"), ","), Topic: topic, GroupID: c.Group, MinBytes: 1, MaxBytes: 2 << 20, CommitInterval: 0})
	defer reader.Close()
	dlq := Writer()
	defer dlq.Close()
	progressed := false
	for ctx.Err() == nil {
		msg, err := reader.FetchMessage(ctx)
		if err != nil {
			if ctx.Err() == nil {
				slog.Error("consumer_fetch_failed", "topic", topic)
			}
			return progressed
		}
		if !c.settle(ctx, topic, msg, handler, dlq) {
			if ctx.Err() == nil {
				slog.Error("consumer_stopped_uncommitted", "topic", topic)
			}
			return progressed
		}
		if err = reader.CommitMessages(ctx, msg); err != nil {
			slog.Error("consumer_commit_failed", "topic", topic)
			return progressed
		}
		progressed = true
	}
	return progressed
}

// settle handles one message, or dead-letters it when it is invalid, and reports whether it may be committed.
func (c Consumer) settle(ctx context.Context, topic string, msg kafka.Message, handler Handler, dlq messageWriter) bool {
	event, valid := decodeEvent(msg.Value, topic)
	if valid {
		completed, invalid := c.handle(ctx, event, handler)
		if completed || !invalid {
			return completed
		}
	}
	err := dlq.WriteMessages(ctx, kafka.Message{Topic: topic + ".dlq", Key: msg.Key, Value: msg.Value})
	slog.Error("invalid_event", "event_id", event.ID, "dead_lettered", err == nil)
	return err == nil
}

// decodeEvent reads an event envelope and reports whether it is a well-formed event of this topic.
func decodeEvent(value []byte, topic string) (Event, bool) {
	var event Event
	if json.Unmarshal(value, &event) != nil {
		return event, false
	}
	return event, ID(event.ID) && event.Type == topic && event.Version == 1 && ID(event.Correlation) && !event.Timestamp.IsZero() && event.Resource != ""
}

// handle runs the handler with retries. It reports whether the event was completed, and whether the handler
// declared it invalid (so it belongs in the dead-letter topic rather than being retried).
func (c Consumer) handle(ctx context.Context, event Event, handler Handler) (completed, invalid bool) {
	for attempt := 0; attempt < 5 && ctx.Err() == nil; attempt++ {
		err := c.handleOnce(ctx, event, handler)
		if err == nil {
			return true, false
		}
		var bad InvalidEvent
		if errors.As(err, &bad) {
			return false, true
		}
		slog.Error("consumer_retry", "event_id", event.ID, "attempt", attempt+1)
		select {
		case <-ctx.Done():
			return false, false
		case <-time.After(time.Duration(1<<attempt) * time.Second):
		}
	}
	return false, false
}

// handleOnce records the event in the inbox and runs the handler in the same transaction; a duplicate is a no-op.
func (c Consumer) handleOnce(ctx context.Context, event Event, handler Handler) error {
	workCtx, cancel := context.WithTimeout(context.WithValue(ctx, correlationKey, event.Correlation), 10*time.Second)
	defer cancel()
	tx, err := c.DB.Begin(workCtx)
	if err != nil {
		return err
	}
	defer tx.Rollback(workCtx)
	result, err := tx.Exec(workCtx, "INSERT INTO inbox(event_id) VALUES($1) ON CONFLICT DO NOTHING", event.ID)
	if err != nil {
		return err
	}
	if result.RowsAffected() > 0 {
		if err = handler(workCtx, tx, event); err != nil {
			return err
		}
	}
	return tx.Commit(workCtx)
}
