package platform

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestServiceTokenFetchesOnceAndWaitersHonourTheirContext(t *testing.T) {
	var fetches atomic.Int32
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fetches.Add(1)
		<-release
		_, _ = w.Write([]byte(`{"access_token":"t1","expires_in":300}`))
	}))
	t.Cleanup(server.Close)
	tokens := &ServiceToken{URL: server.URL, Client: server.Client()}
	first := make(chan string)
	go func() { token, _ := tokens.Get(context.Background()); first <- token }()
	for fetches.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	// A caller with a short deadline gives up while the slow fetch runs, instead of waiting for it.
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if _, err := tokens.Get(ctx); err == nil {
		t.Fatal("a waiter outlived its context")
	}
	close(release)
	if token := <-first; token != "t1" {
		t.Fatalf("token %q", token)
	}
	if token, err := tokens.Get(context.Background()); err != nil || token != "t1" || fetches.Load() != 1 {
		t.Fatalf("%q %v after %d fetches", token, err, fetches.Load())
	}
}

func TestAShortLivedTokenIsStillReused(t *testing.T) {
	var fetches atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fetches.Add(1)
		_, _ = w.Write([]byte(`{"access_token":"t1","expires_in":20}`))
	}))
	t.Cleanup(server.Close)
	tokens := &ServiceToken{URL: server.URL, Client: server.Client()}
	for range 3 {
		if _, err := tokens.Get(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if fetches.Load() != 1 {
		t.Fatalf("a 20-second token was fetched %d times", fetches.Load())
	}
}

func TestTheFetchOutlivesTheCallerThatStartedIt(t *testing.T) {
	var fetches atomic.Int32
	release := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fetches.Add(1)
		<-release
		_, _ = w.Write([]byte(`{"access_token":"t1","expires_in":300}`))
	}))
	t.Cleanup(server.Close)
	tokens := &ServiceToken{URL: server.URL, Client: server.Client()}
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error)
	go func() { _, err := tokens.Get(ctx); first <- err }()
	for fetches.Load() == 0 {
		time.Sleep(time.Millisecond)
	}
	second := make(chan string)
	go func() { token, _ := tokens.Get(context.Background()); second <- token }()
	cancel() // the caller that started the fetch gives up; the one still waiting gets the token
	if err := <-first; err == nil {
		t.Fatal("a cancelled caller got a token")
	}
	close(release)
	if token := <-second; token != "t1" || fetches.Load() != 1 {
		t.Fatalf("token %q after %d fetches", token, fetches.Load())
	}
}
