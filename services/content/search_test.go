package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestFuseRanksByReciprocalRankThenBestLegThenID(t *testing.T) {
	vector := []hit{{ChunkID: "a"}, {ChunkID: "b"}, {ChunkID: "c"}}
	keyword := []hit{{ChunkID: "c"}, {ChunkID: "d"}}
	got := fuse(vector, keyword)
	ids := []string{}
	for _, h := range got {
		ids = append(ids, h.ChunkID)
	}
	// c is in both legs; a (vector 1) and b (vector 2) and d (keyword 2) follow by single-leg rank, b before d by id.
	if strings.Join(ids, ",") != "c,a,b,d" {
		t.Fatalf("order %v", ids)
	}
	if got[0].Score != 1.0/63+1.0/61 || got[0].vectorRank != 3 || got[0].keywordRank != 1 {
		t.Fatalf("c scored %v (ranks %d,%d)", got[0].Score, got[0].vectorRank, got[0].keywordRank)
	}
}

func TestFuseKeepsThirty(t *testing.T) {
	var rows []hit
	for i := 0; i < 50; i++ {
		rows = append(rows, hit{ChunkID: string(rune('A' + i))})
	}
	if got := fuse(rows, nil); len(got) != rrfKeep || got[0].ChunkID != "A" {
		t.Fatalf("kept %d, first %s", len(got), got[0].ChunkID)
	}
}

func TestDistinctKeepsTheBestRankedCopy(t *testing.T) {
	hits := []hit{{ChunkID: "a", Content: "Learning Journey\nWeek 1"}, {ChunkID: "b", Content: "Other"}, {ChunkID: "c", Content: " Learning Journey  Week 1 "}}
	got := distinct(hits)
	if len(got) != 2 || got[0].ChunkID != "a" || got[1].ChunkID != "b" {
		t.Fatalf("got %+v", got)
	}
	if len(hits) != 3 || hits[2].ChunkID != "c" {
		t.Fatal("the input slice was modified")
	}
}

func TestBackoffDoublesToThirtyMinutes(t *testing.T) {
	want := []time.Duration{30 * time.Second, time.Minute, 2 * time.Minute, 4 * time.Minute, 8 * time.Minute, 16 * time.Minute, 30 * time.Minute, 30 * time.Minute}
	for i, w := range want {
		if got := backoff(i + 1); got != w {
			t.Fatalf("attempt %d: %v, want %v", i+1, got, w)
		}
	}
}

func TestTransient(t *testing.T) {
	for _, err := range []error{&aiError{Status: 0, Code: "UNREACHABLE"}, &aiError{Status: http.StatusServiceUnavailable, Code: "AI_PROVIDER_NOT_CONFIGURED"}, &aiError{Status: http.StatusBadGateway, Code: "AI_PROVIDER_FAILED"}, &aiError{Status: http.StatusTooManyRequests, Code: ""}, &aiError{Status: http.StatusUnauthorized, Code: ""}, errors.New("storage down")} {
		if !transient(err) {
			t.Fatalf("%v should be retried", err)
		}
	}
	for _, err := range []error{&aiError{Status: http.StatusUnprocessableEntity, Code: "VALIDATION_ERROR"}, &aiError{Status: http.StatusRequestEntityTooLarge, Code: "PAYLOAD_TOO_LARGE"}, &aiError{Status: http.StatusForbidden, Code: "FORBIDDEN"}} {
		if transient(err) {
			t.Fatalf("%v will fail again", err)
		}
	}
}

func TestVectorTextAndTruncate(t *testing.T) {
	if got := vectorText([]float32{0.5, -1, 1e-7}); got != "[0.5,-1,1e-07]" {
		t.Fatalf("%q", got)
	}
	if got := truncate("ééé", 3); got != "é" {
		t.Fatalf("cut inside a rune: %q", got)
	}
}

func goodResult() ingestResult {
	pages, model, image := 3, "text-embedding-3-small", "0123456789abcdef.png"
	embedding := make([]float32, dimensions)
	return ingestResult{
		Outcome: "indexed", PageCount: &pages, EmbeddingModel: &model,
		Chunks: []ingestChunk{
			{Ordinal: 0, Content: "Leaves", EmbedText: "Biology\n\nLeaves", PageStart: 1, PageEnd: 1, TokenCount: 2, Kind: "text", Source: "pdf", Cites: []string{"figure 1"}, Embedding: embedding},
			{Ordinal: 1, Content: "Figure 1: a leaf", EmbedText: "Biology\n\nFigure 1", PageStart: 2, PageEnd: 2, TokenCount: 4, Kind: "figure", Source: "vision", Image: &image, Embedding: embedding},
		},
		Figures: []ingestFigure{{Image: image, PNG: []byte("png")}},
	}
}

func TestValidateRejectsWhatCannotBeStored(t *testing.T) {
	if err := validate(goodResult()); err != nil {
		t.Fatal(err)
	}
	cases := map[string]func(*ingestResult){
		"no chunks":      func(r *ingestResult) { r.Chunks = nil },
		"no model":       func(r *ingestResult) { r.EmbeddingModel = nil },
		"wrong size":     func(r *ingestResult) { r.Chunks[0].Embedding = make([]float32, 3) },
		"gap":            func(r *ingestResult) { r.Chunks[1].Ordinal = 5 },
		"missing figure": func(r *ingestResult) { r.Figures = nil },
		"bad figure":     func(r *ingestResult) { r.Figures[0].Image = "../x.png" },
	}
	for name, change := range cases {
		r := goodResult()
		change(&r)
		if validate(r) == nil {
			t.Fatalf("%s accepted", name)
		}
	}
}

// fakeAICore is ai-core and Keycloak's token endpoint for the client tests.
type fakeAICore struct {
	*httptest.Server
	tokens  atomic.Int32
	ingest  func(w http.ResponseWriter, r *http.Request)
	scores  func(w http.ResponseWriter, r *http.Request)
	embeds  atomic.Int32
	reranks atomic.Int32
}

func newFakeAICore(t *testing.T) *fakeAICore {
	f := &fakeAICore{}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/token" {
			if r.PostFormValue("client_id") != "content-indexer" || r.PostFormValue("client_secret") != "secret" {
				w.WriteHeader(http.StatusUnauthorized)
				return
			}
			f.tokens.Add(1)
			json.NewEncoder(w).Encode(map[string]any{"access_token": "token-" + string(rune('0'+f.tokens.Load())), "expires_in": 300})
			return
		}
		if r.Header.Get("Authorization") == "" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		switch r.URL.Path {
		case "/v1/ingest":
			f.ingest(w, r)
		case "/v1/inference":
			f.embeds.Add(1)
			json.NewEncoder(w).Encode(map[string]any{"vectors": [][]float32{unit()}, "model_version": "text-embedding-3-small"})
		case "/v1/rerank":
			f.reranks.Add(1)
			f.scores(w, r)
		}
	}))
	t.Cleanup(f.Close)
	return f
}

func (f *fakeAICore) client() *aiCore {
	return &aiCore{base: f.URL, tokens: &tokenSource{url: f.URL + "/token", id: "content-indexer", secret: "secret", client: f.Client()}, client: f.Client()}
}

func TestIngestSendsThePDFAndDecodesFigures(t *testing.T) {
	f := newFakeAICore(t)
	f.ingest = func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Header.Get("Content-Type") != "application/pdf" || string(body) != "%PDF-1.7" || r.URL.Query().Get("title") != "Biology & more" || r.Header.Get("Authorization") != "Bearer token-1" {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"outcome": "indexed", "figures": []map[string]string{{"image": "0123456789abcdef.png", "png_base64": "cG5n"}}})
	}
	ai := f.client()
	for i := 0; i < 2; i++ {
		result, err := ai.ingest(t.Context(), "Biology & more", strings.NewReader("%PDF-1.7"), 8, testBilled)
		if err != nil || result.Outcome != "indexed" || string(result.Figures[0].PNG) != "png" {
			t.Fatalf("%+v %v", result, err)
		}
	}
	if f.tokens.Load() != 1 {
		t.Fatalf("token fetched %d times, want once (cached)", f.tokens.Load())
	}
}

func TestErrorsCarryStatusAndCodeAndRefusedTokensAreDropped(t *testing.T) {
	f := newFakeAICore(t)
	f.ingest = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		w.Write([]byte(`{"error":{"code":"UNAUTHENTICATED","message":"x"}}`))
	}
	ai := f.client()
	_, err := ai.ingest(t.Context(), "t", strings.NewReader("x"), 1, testBilled)
	var e *aiError
	if !errors.As(err, &e) || e.Status != http.StatusUnauthorized || e.Code != "UNAUTHENTICATED" || !transient(err) {
		t.Fatalf("%v", err)
	}
	ai.ingest(t.Context(), "t", strings.NewReader("x"), 1, testBilled)
	if f.tokens.Load() != 2 {
		t.Fatalf("a refused token was reused")
	}
	f.Close()
	if _, err = ai.ingest(t.Context(), "t", strings.NewReader("x"), 1, testBilled); !transient(err) {
		t.Fatalf("ai-core down is not retried: %v", err)
	}
}

func TestRerankRejectsAShortScoreList(t *testing.T) {
	f := newFakeAICore(t)
	f.scores = func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"scores": []float64{0.5}})
	}
	if _, err := f.client().rerank(t.Context(), "q", []passage{{Title: "T", Content: "a"}, {Title: "T", Content: "b"}}, testBilled); err == nil {
		t.Fatal("scores for 1 of 2 documents accepted")
	}
}

func TestRerankSendsThePassagePartsForAICoreToJoin(t *testing.T) {
	f := newFakeAICore(t)
	var sent struct {
		Passages []map[string]any `json:"passages"`
	}
	f.scores = func(w http.ResponseWriter, r *http.Request) {
		json.NewDecoder(r.Body).Decode(&sent)
		json.NewEncoder(w).Encode(map[string]any{"scores": []float64{0.5}})
	}
	section := "Chapter 1 > Leaves"
	if _, err := f.client().rerank(t.Context(), "q", []passage{{"Biology", &section, "Stomata"}}, testBilled); err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"title": "Biology", "section_path": section, "content": "Stomata"}
	if len(sent.Passages) != 1 || fmt.Sprint(sent.Passages[0]) != fmt.Sprint(want) {
		t.Fatalf("sent %v", sent.Passages)
	}
}

// testBilled is the user test calls to ai-core are made for.
var testBilled = billed{"teacher-1", "org", "teacher"}

func TestTheBilledUserIsSentAndTheSpendLimitIsRecognised(t *testing.T) {
	f := newFakeAICore(t)
	f.ingest = func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("billed_user") != "teacher-1" || q.Get("billed_org") != "org" || q.Get("billed_role") != "teacher" {
			w.WriteHeader(http.StatusUnprocessableEntity)
			return
		}
		w.WriteHeader(http.StatusTooManyRequests)
		w.Write([]byte(`{"error":{"code":"AI_SPEND_LIMIT","message":"used up","resets_at":"2026-10-14T00:00:00+08:00"}}`))
	}
	_, err := f.client().ingest(t.Context(), "t", strings.NewReader("x"), 1, testBilled)
	limit, ok := spendLimit(err)
	if !ok || limit.Message != "used up" || limit.ResetsAt.IsZero() {
		t.Fatalf("%#v", err)
	}
	if _, ok = spendLimit(&aiError{Status: http.StatusTooManyRequests, Code: "RATE_LIMITED"}); ok {
		t.Fatal("a provider rate limit is not the user's allowance")
	}
}

func TestCallerAllowance(t *testing.T) {
	c := platformtest.Claims("u", "student")
	if caller(c).Role != "student" {
		t.Fatal("student billed as teacher")
	}
	c.Realm.Roles = []string{"teacher", "student"}
	if caller(c).Role != "teacher" {
		t.Fatal("teacher billed as student")
	}
}
