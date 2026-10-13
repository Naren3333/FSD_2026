package platform

import (
	"errors"
	"testing"

	"github.com/segmentio/kafka-go"
)

func TestDeliveryFailures(t *testing.T) {
	if got := deliveryFailures(nil, 2); got[0] || got[1] {
		t.Fatal("a successful write failed messages")
	}
	if got := deliveryFailures(errors.New("broker down"), 2); !got[0] || !got[1] {
		t.Fatal("a whole-batch error must fail every message")
	}
	got := deliveryFailures(kafka.WriteErrors{nil, errors.New("leader moved"), nil}, 3)
	if got[0] || !got[1] || got[2] {
		t.Fatalf("per-message errors mapped wrongly: %v", got)
	}
}
