package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"learning/platform"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// These tests run the indexing worker and search against real PostgreSQL with pgvector, in a throwaway schema,
// and a fake ai-core. They need CONTENT_TEST_DATABASE_URL (see docs/development/operations.md) and are skipped
// without it.

type memoryStore struct {
	mu      sync.Mutex
	objects map[string][]byte
}

func (m *memoryStore) open(ctx context.Context, key string) (io.ReadCloser, int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	data, ok := m.objects[key]
	if !ok {
		return nil, 0, errors.New("no such object")
	}
	return io.NopCloser(bytes.NewReader(data)), int64(len(data)), nil
}

func (m *memoryStore) put(ctx context.Context, key string, data []byte, contentType string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.objects[key] = data
	return nil
}

func testDB(t *testing.T) *pgxpool.Pool {
	url := os.Getenv("CONTENT_TEST_DATABASE_URL")
	if url == "" {
		t.Skip("CONTENT_TEST_DATABASE_URL is not set")
	}
	ctx := context.Background()
	schema := "test_" + strings.ReplaceAll(uuid.NewString(), "-", "")[:12]
	admin, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		admin.Exec(context.Background(), "DROP SCHEMA "+schema+" CASCADE")
		admin.Close()
	})
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema + ",public"
	db, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	ordered, err := platform.Migrations(migrations)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range ordered {
		if _, err = db.Exec(ctx, m.SQL); err != nil {
			t.Fatalf("%s: %v", m.Name, err)
		}
	}
	if _, err = db.Exec(ctx, platform.EventMigration); err != nil {
		t.Fatal(err)
	}
	return db
}

type fixture struct {
	t     *testing.T
	db    *pgxpool.Pool
	store *memoryStore
	ai    *fakeAICore
	x     indexer
	s     server
	org   string
	room  string
}

func newFixture(t *testing.T) *fixture {
	db := testDB(t)
	f := &fixture{t: t, db: db, store: &memoryStore{objects: map[string][]byte{}}, ai: newFakeAICore(t), org: "org-1", room: uuid.NewString()}
	f.x = indexer{db, f.store, f.ai.client(), 0}
	f.s = server{store: pgStore{db}, ai: f.ai.client()}
	f.ai.ingest = f.reply(indexedResult())
	f.ai.scores = func(w http.ResponseWriter, r *http.Request) {
		var body struct{ Documents []string }
		json.NewDecoder(r.Body).Decode(&body)
		scores := make([]float64, len(body.Documents))
		for i := range scores {
			scores[i] = float64(i) // reverses the RRF order
		}
		json.NewEncoder(w).Encode(map[string]any{"scores": scores})
	}
	return f
}

func unit() []float32 {
	v := make([]float32, dimensions)
	v[0] = 1
	return v
}

func indexedResult() ingestResult {
	r := goodResult()
	label := "figure 1"
	r.Chunks[1].Label = &label
	for i := range r.Chunks {
		r.Chunks[i].Embedding = unit()
	}
	return r
}

func (f *fixture) reply(result any) func(http.ResponseWriter, *http.Request) {
	return func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		json.NewEncoder(w).Encode(result)
	}
}

// upload stores a PDF document waiting for the indexer, as the upload handler does.
func (f *fixture) upload(docType string) string {
	id := uuid.NewString()
	key := f.org + "/" + id + "/1"
	f.store.put(context.Background(), key, []byte("%PDF-1.7"), "application/pdf")
	_, err := f.s.store.insertDocument(context.Background(), id, f.room, f.org, "teacher", "Biology", "application/pdf", key, strings.Repeat("a", 64), "indexing", docType, []Chunk{})
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

func (f *fixture) value(query string, args ...any) string {
	var v string
	if err := f.db.QueryRow(context.Background(), "SELECT ("+query+")::text", args...).Scan(&v); err != nil {
		f.t.Fatalf("%s: %v", query, err)
	}
	return v
}

func (f *fixture) runAll() {
	for {
		found, err := f.x.next(context.Background())
		if err != nil {
			f.t.Fatal(err)
		}
		if !found {
			return
		}
	}
}

func TestIndexingStoresChunksFiguresAndOneEvent(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	if got := f.value("SELECT count(*) FROM outbox WHERE topic='content.uploaded.v1'"); got != "1" {
		t.Fatalf("uploaded events: %s", got)
	}
	f.runAll()
	if got := f.value("SELECT status||' '||page_count||' '||chunk_count||' '||embedding_model FROM documents WHERE id=$1", id); got != "indexed 3 2 text-embedding-3-small" {
		t.Fatal(got)
	}
	if got := f.value("SELECT string_agg(kind||':'||coalesce(label,'-')||':'||array_to_string(cites,'|'),',' ORDER BY ordinal) FROM document_chunks WHERE document_id=$1", id); got != "text:-:figure 1,figure:figure 1:" {
		t.Fatal(got)
	}
	if got := f.value("SELECT count(*) FROM index_jobs"); got != "0" {
		t.Fatal("job left behind")
	}
	if string(f.store.objects[figureKey(f.org, id, "0123456789abcdef.png")]) != "png" {
		t.Fatal("figure crop not stored")
	}
	var body []byte
	f.db.QueryRow(context.Background(), "SELECT body FROM outbox WHERE topic='content.indexed.v1'").Scan(&body)
	var event struct {
		Resource string         `json:"resource_id"`
		Data     map[string]any `json:"data"`
	}
	json.Unmarshal(body, &event)
	if event.Resource != id || event.Data["chunk_count"] != 2.0 || event.Data["classroom_id"] != f.room || event.Data["doc_type"] != "teaching_material" {
		t.Fatalf("event %s", body)
	}
}

func TestADuplicateRunCannotOverwriteOrEmitTwice(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	ctx := context.Background()
	first, err := f.x.claim(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// The first run outlives its lease and a second worker takes the job over.
	f.db.Exec(ctx, "UPDATE index_jobs SET next_attempt=now()")
	second, err := f.x.claim(ctx)
	if err != nil || second.document != id || second.attempts != 2 {
		t.Fatalf("takeover: %+v %v", second, err)
	}
	// The first run returns while the second is still working, with a different result: it lost its lease, so
	// nothing changes and the job stays with the second run.
	stale := indexedResult()
	stale.Chunks = stale.Chunks[:1]
	stale.Chunks[0].Cites = nil
	f.ai.ingest = f.reply(stale)
	if err = f.x.process(ctx, first); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT status||' '||(SELECT count(*) FROM document_chunks)||' '||(SELECT lease FROM index_jobs WHERE document_id=$1) FROM documents WHERE id=$1", id); got != "indexing 0 "+second.lease {
		t.Fatalf("status, chunks, lease: %s", got)
	}
	f.ai.ingest = f.reply(indexedResult())
	if err = f.x.process(ctx, second); err != nil {
		t.Fatal(err)
	}
	f.ai.ingest = f.reply(stale)
	if err = f.x.process(ctx, first); err != nil {
		t.Fatal(err)
	}
	if got := f.value("SELECT chunk_count||' '||(SELECT count(*) FROM document_chunks)||' '||(SELECT count(*) FROM outbox WHERE topic='content.indexed.v1') FROM documents WHERE id=$1", id); got != "2 2 1" {
		t.Fatalf("chunk_count, chunks, events: %s", got)
	}
	// A failure reported by the stale run is discarded too.
	f.ai.ingest = f.reply(map[string]any{"outcome": "failed", "failure_reason": "CORRUPT"})
	f.x.process(ctx, first)
	if got := f.value("SELECT status FROM documents WHERE id=$1", id); got != "indexed" {
		t.Fatal(got)
	}
}

func TestAnOutageIsRetriedThenFailsTheDocument(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.ai.ingest = func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		w.Write([]byte(`{"error":{"code":"AI_PROVIDER_NOT_CONFIGURED","message":"x"}}`))
	}
	f.runAll()
	if got := f.value("SELECT attempts||' '||(lease IS NULL)||' '||(next_attempt>now()+interval '20 seconds')||' '||last_error FROM index_jobs WHERE document_id=$1", id); got != "1 true true ai-core 503 AI_PROVIDER_NOT_CONFIGURED" {
		t.Fatal(got)
	}
	if got := f.value("SELECT status FROM documents WHERE id=$1", id); got != "indexing" {
		t.Fatal(got)
	}
	for i := 1; i < maxAttempts; i++ {
		f.db.Exec(context.Background(), "UPDATE index_jobs SET next_attempt=now()")
		f.runAll()
	}
	if got := f.value("SELECT status||' '||failure_reason FROM documents WHERE id=$1", id); got != "failed AI_UNAVAILABLE" {
		t.Fatal(got)
	}
	if got := f.value("SELECT count(*) FROM index_jobs"); got != "0" {
		t.Fatal("job left after giving up")
	}
}

func TestAnUnreadablePDFFailsWithItsReasonAndReindexRecovers(t *testing.T) {
	f := newFixture(t)
	id := f.upload("teaching_material")
	f.runAll()
	ctx := context.Background()
	if queued, err := f.s.store.queueIndex(ctx, f.org, id); err != nil || !queued {
		t.Fatalf("reindex: %v %v", queued, err)
	}
	if queued, _ := f.s.store.queueIndex(ctx, f.org, id); queued {
		t.Fatal("a document already indexing was queued twice")
	}
	if got := f.value("SELECT count(*) FROM document_chunks"); got != "2" {
		t.Fatal("earlier chunks must stay searchable while reindexing")
	}
	f.ai.ingest = f.reply(map[string]any{"outcome": "failed", "failure_reason": "ENCRYPTED", "failure_detail": "password"})
	f.runAll()
	if got := f.value("SELECT status||' '||failure_reason||' '||failure_detail||' '||(SELECT count(*) FROM document_chunks) FROM documents WHERE id=$1", id); got != "failed ENCRYPTED password 0" {
		t.Fatal(got)
	}
	f.ai.ingest = f.reply(indexedResult())
	f.s.store.queueIndex(ctx, f.org, id)
	f.runAll()
	if got := f.value("SELECT status||' '||(failure_reason IS NULL) FROM documents WHERE id=$1", id); got != "indexed true" {
		t.Fatal(got)
	}
}

func TestSearchFusesRerankRelatedAndAccess(t *testing.T) {
	f := newFixture(t)
	notes := f.upload("teaching_material")
	f.upload("rubric")
	f.runAll()
	ctx := context.Background()
	all := filter{f.org, f.room, nil, nil}
	plain, err := f.s.runSearch(ctx, all, "leaves", 30, false, testBilled)
	if err != nil {
		t.Fatal(err)
	}
	if len(plain.Results) != 4 || plain.RerankFallback {
		t.Fatalf("%d results", len(plain.Results))
	}
	reranked, err := f.s.runSearch(ctx, all, "leaves", 30, true, testBilled)
	if err != nil || reranked.RerankFallback || reranked.Results[0].ChunkID != plain.Results[3].ChunkID || reranked.Results[0].Score != 3 {
		t.Fatalf("rerank did not reorder: %v", err)
	}
	students := filter{f.org, f.room, []string{"teaching_material"}, nil}
	only, _ := f.s.runSearch(ctx, students, "leaves", 30, false, testBilled)
	for _, h := range only.Results {
		if h.DocType != "teaching_material" || h.DocumentID != notes {
			t.Fatalf("a student filter returned %s", h.DocType)
		}
	}
	var text hit
	for _, h := range only.Results {
		if h.Kind == "text" {
			text = h
		}
	}
	if len(text.Related) != 1 || *text.Related[0].Label != "figure 1" || *text.Related[0].Image != "0123456789abcdef.png" {
		t.Fatalf("related %+v", text.Related)
	}
	other, _ := f.s.runSearch(ctx, filter{f.org, uuid.NewString(), nil, nil}, "leaves", 30, false, testBilled)
	if len(other.Results) != 0 {
		t.Fatal("another classroom's chunks were returned")
	}
	f.ai.scores = func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) }
	fallback, err := f.s.runSearch(ctx, all, "leaves", 2, true, testBilled)
	if err != nil || !fallback.RerankFallback || len(fallback.Results) != 2 || fallback.Results[0].ChunkID != plain.Results[0].ChunkID {
		t.Fatalf("fallback: %v", err)
	}
}
