package main

import (
	"encoding/json"
	"errors"
	"io"
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

func TestPassageTextMatchesIngest(t *testing.T) {
	section := "Chapter 1 > Leaves"
	if got := passageText("Biology", &section, "Stomata"); got != "Biology\nLeaves\n\nStomata" {
		t.Fatalf("%q", got)
	}
	if got := passageText("Biology", nil, "Stomata"); got != "Biology\n\nStomata" {
		t.Fatalf("%q", got)
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
	for _, err := range []error{&aiError{0, "UNREACHABLE"}, &aiError{503, "AI_PROVIDER_NOT_CONFIGURED"}, &aiError{502, "AI_PROVIDER_FAILED"}, &aiError{429, ""}, &aiError{401, ""}, errors.New("storage down")} {
		if !transient(err) {
			t.Fatalf("%v should be retried", err)
		}
	}
	for _, err := range []error{&aiError{422, "VALIDATION_ERROR"}, &aiError{413, "PAYLOAD_TOO_LARGE"}, &aiError{403, "FORBIDDEN"}} {
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
				w.WriteHeader(401)
				return
			}
			f.tokens.Add(1)
			json.NewEncoder(w).Encode(map[string]any{"access_token": "token-" + string(rune('0'+f.tokens.Load())), "expires_in": 300})
			return
		}
		if r.Header.Get("Authorization") == "" {
			w.WriteHeader(401)
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
			w.WriteHeader(400)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"outcome": "indexed", "figures": []map[string]string{{"image": "0123456789abcdef.png", "png_base64": "cG5n"}}})
	}
	ai := f.client()
	for i := 0; i < 2; i++ {
		result, err := ai.ingest(t.Context(), "Biology & more", strings.NewReader("%PDF-1.7"), 8)
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
		w.WriteHeader(401)
		w.Write([]byte(`{"error":{"code":"UNAUTHENTICATED","message":"x"}}`))
	}
	ai := f.client()
	_, err := ai.ingest(t.Context(), "t", strings.NewReader("x"), 1)
	if e, ok := err.(*aiError); !ok || e.Status != 401 || e.Code != "UNAUTHENTICATED" || !transient(err) {
		t.Fatalf("%v", err)
	}
	ai.ingest(t.Context(), "t", strings.NewReader("x"), 1)
	if f.tokens.Load() != 2 {
		t.Fatalf("a refused token was reused")
	}
	f.Close()
	if _, err = ai.ingest(t.Context(), "t", strings.NewReader("x"), 1); !transient(err) {
		t.Fatalf("ai-core down is not retried: %v", err)
	}
}

func TestRerankRejectsAShortScoreList(t *testing.T) {
	f := newFakeAICore(t)
	f.scores = func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"scores": []float64{0.5}})
	}
	if _, err := f.client().rerank(t.Context(), "q", []string{"a", "b"}); err == nil {
		t.Fatal("scores for 1 of 2 documents accepted")
	}
}
