package platform

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/segmentio/kafka-go"
)

// EventMigration creates the outbox and inbox tables; services that publish or consume events pass it to New.
const EventMigration = `
CREATE TABLE IF NOT EXISTS outbox (id uuid PRIMARY KEY, topic text NOT NULL, partition_key text NOT NULL, body jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz, attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS inbox (event_id uuid PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());`

// Event is the envelope of every business event on Kafka.
type Event struct {
	ID          string          `json:"event_id"`
	Type        string          `json:"event_type"`
	Version     int             `json:"schema_version"`
	Timestamp   time.Time       `json:"timestamp"`
	Correlation string          `json:"correlation_id"`
	Resource    string          `json:"resource_id"`
	Data        json.RawMessage `json:"data"`
}

// Emit writes an event to the outbox inside the caller's transaction, so it is published only if the change commits.
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

// Writer sends synchronously with every replica's acknowledgement. BatchTimeout matters: kafka-go otherwise holds
// a partial batch for a second before sending it, so every write took about a second.
func Writer() *kafka.Writer {
	return &kafka.Writer{Addr: kafka.TCP(strings.Split(Required("KAFKA_BROKERS"), ",")...), Balancer: &kafka.Hash{}, RequiredAcks: kafka.RequireAll, MaxAttempts: 3, BatchTimeout: 10 * time.Millisecond, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
}

// KafkaReady is a readiness check: the first broker answers a metadata request.
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

// InvalidEvent is a handler's verdict that an event can never be processed: it is dead-lettered, not retried.
type InvalidEvent struct{ Reason string }

func (e InvalidEvent) Error() string { return e.Reason }

// Handler applies one event inside the transaction that also records it in the inbox.
type Handler func(ctx context.Context, tx pgx.Tx, event Event) error
