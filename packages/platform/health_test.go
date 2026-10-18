package platform

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestCheckReadyNeedsOK(t *testing.T) {
	var status atomic.Int32
	status.Store(http.StatusOK)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health/ready" {
			t.Errorf("asked %s", r.URL.Path)
		}
		w.WriteHeader(int(status.Load()))
	}))
	defer server.Close()
	if err := checkReady(server.URL + "/health/ready"); err != nil {
		t.Fatalf("ready service: %v", err)
	}
	status.Store(http.StatusServiceUnavailable)
	if err := checkReady(server.URL + "/health/ready"); err == nil {
		t.Fatal("a 503 counted as ready")
	}
	server.Close()
	if err := checkReady(server.URL + "/health/ready"); err == nil {
		t.Fatal("a closed port counted as ready")
	}
}

func TestHealthcheckReturnsWithoutTheFlag(t *testing.T) {
	// The test binary's own arguments never include the flag, so this must return instead of exiting.
	Healthcheck()
}
