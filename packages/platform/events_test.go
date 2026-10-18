package platform

import (
	"encoding/json"
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

func TestDecodeClassroomDeletedRejectsMalformedDeletions(t *testing.T) {
	deleted, other := "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f", "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	for _, data := range []string{`{}`, `not json`, `{"classroom_id":"x","org_id":"o"}`, `{"classroom_id":"` + other + `","org_id":"o"}`, `{"classroom_id":"` + deleted + `","org_id":""}`} {
		_, err := DecodeClassroomDeleted(Event{Resource: deleted, Data: json.RawMessage(data)})
		var invalid InvalidEvent
		if !errors.As(err, &invalid) {
			t.Errorf("%s was not rejected: %v", data, err)
		}
	}
	v, err := DecodeClassroomDeleted(Event{Resource: deleted, Data: json.RawMessage(`{"classroom_id":"` + deleted + `","org_id":"o"}`)})
	if err != nil || v.Classroom != deleted || v.Org != "o" {
		t.Fatalf("%+v %v", v, err)
	}
}

func TestDecodeEnrollmentRemovedRejectsMalformedRemovals(t *testing.T) {
	classroom, other := "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f", "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	for _, data := range []string{`{}`, `not json`, `{"classroom_id":"` + classroom + `","org_id":"o"}`, `{"classroom_id":"` + other + `","org_id":"o","student_id":"s"}`, `{"classroom_id":"` + classroom + `","org_id":"","student_id":"s"}`} {
		_, err := DecodeEnrollmentRemoved(Event{Resource: classroom, Data: json.RawMessage(data)})
		var invalid InvalidEvent
		if !errors.As(err, &invalid) {
			t.Errorf("%s was not rejected: %v", data, err)
		}
	}
	v, err := DecodeEnrollmentRemoved(Event{Resource: classroom, Data: json.RawMessage(`{"classroom_id":"` + classroom + `","org_id":"o","student_id":"s"}`)})
	if err != nil || v.Classroom != classroom || v.Org != "o" || v.Student != "s" {
		t.Fatalf("%+v %v", v, err)
	}
}

func TestDecodeUserDeletedRejectsMalformedDeletions(t *testing.T) {
	user, other := "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f", "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	for _, data := range []string{`{}`, `not json`, `{"user_id":"` + user + `","org_id":"o"}`, `{"user_id":"` + other + `","org_id":"o","role":"student"}`, `{"user_id":"` + user + `","org_id":"","role":"student"}`, `{"user_id":"` + user + `","org_id":"o","role":"admin"}`} {
		_, err := DecodeUserDeleted(Event{Resource: user, Data: json.RawMessage(data)})
		var invalid InvalidEvent
		if !errors.As(err, &invalid) {
			t.Errorf("%s was not rejected: %v", data, err)
		}
	}
	v, err := DecodeUserDeleted(Event{Resource: user, Data: json.RawMessage(`{"user_id":"` + user + `","org_id":"o","role":"teacher"}`)})
	if err != nil || v.User != user || v.Org != "o" || v.Role != "teacher" {
		t.Fatalf("%+v %v", v, err)
	}
}
