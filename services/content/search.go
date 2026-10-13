package main

import (
	"context"
	"fmt"
	"learning/platform"
	"log/slog"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
)

// Hybrid search, ported from fsd_test's knowledge service: a vector leg and a keyword leg under the caller's
// access filter, merged with reciprocal rank fusion, then optionally reranked by ai-core. If the rerank fails the
// results stay in RRF order and rerank_fallback is set. A result that cites a figure or table of its document
// ("as Figure 7 shows") comes back with that chunk in `related`.
const (
	legLimit      = 50 // candidates per leg
	rrfK          = 60
	rrfKeep       = 30 // fused candidates kept: the reranker's input, and the top_k cap
	efSearch      = 100
	maxQuery      = 1000
	embedTimeout  = 8 * time.Second
	rerankTimeout = 4 * time.Second
)

type searchRequest struct {
	Classroom   string   `json:"classroom_id"`
	Query       string   `json:"query"`
	TopK        *int     `json:"top_k"`
	Rerank      *bool    `json:"rerank"`
	DocumentIDs []string `json:"document_ids"`
	DocTypes    []string `json:"doc_types"`
}

type related struct {
	ChunkID   string  `json:"chunk_id"`
	Kind      string  `json:"kind"`
	Label     *string `json:"label"`
	PageStart int     `json:"page_start"`
	Image     *string `json:"image"`
	Content   string  `json:"content"`
}

type hit struct {
	ChunkID     string    `json:"chunk_id"`
	DocumentID  string    `json:"document_id"`
	Title       string    `json:"title"`
	DocType     string    `json:"doc_type"`
	SectionPath *string   `json:"section_path"`
	PageStart   int       `json:"page_start"`
	PageEnd     int       `json:"page_end"`
	Content     string    `json:"content"`
	Kind        string    `json:"kind"`
	Source      string    `json:"source"`
	Image       *string   `json:"image"`
	Score       float64   `json:"score"`
	Related     []related `json:"related"`
	cites       []string
	vectorRank  int
	keywordRank int
	rrf         float64
}

type searchResponse struct {
	Results        []hit `json:"results"`
	RerankFallback bool  `json:"rerank_fallback"`
}

// filter is the access filter (org and classroom, and teaching material only for students) narrowed by the
// request's own filters, which can never widen it.
type filter struct {
	org, classroom string
	docTypes       []string
	documents      []string
}

const filterSQL = "org_id=$1 AND classroom_id=$2 AND (coalesce(cardinality($3::text[]),0)=0 OR doc_type=ANY($3)) AND (coalesce(cardinality($4::uuid[]),0)=0 OR document_id=ANY($4))"

func (f filter) args(more ...any) []any {
	return append([]any{f.org, f.classroom, f.docTypes, f.documents}, more...)
}

func (s server) search(w http.ResponseWriter, r *http.Request) {
	var req searchRequest
	if !platform.Decode(w, r, &req) {
		return
	}
	req.Query = strings.TrimSpace(req.Query)
	topK, rerank := 8, true
	if req.TopK != nil {
		topK = *req.TopK
	}
	if req.Rerank != nil {
		rerank = *req.Rerank
	}
	if req.Query == "" || utf8.RuneCountInString(req.Query) > maxQuery || topK < 1 || topK > rrfKeep || len(req.DocumentIDs) > 100 {
		platform.Error(w, 422, "VALIDATION_ERROR", "Send a query of 1–1000 characters, top_k 1–30 and at most 100 document ids.")
		return
	}
	for _, id := range req.DocumentIDs {
		if !platform.ID(id) {
			platform.Error(w, 422, "VALIDATION_ERROR", "Document ids must be UUIDs.")
			return
		}
	}
	for _, t := range req.DocTypes {
		if !docTypes[t] {
			platform.Error(w, 422, "VALIDATION_ERROR", "Unknown document type.")
			return
		}
	}
	if !s.Access(w, r, req.Classroom, "") {
		return
	}
	f := filter{platform.Actor(r).Org, req.Classroom, req.DocTypes, req.DocumentIDs}
	if !platform.Actor(r).Has("teacher") {
		if len(f.docTypes) > 0 && !contains(f.docTypes, "teaching_material") {
			platform.JSON(w, 200, searchResponse{Results: []hit{}})
			return
		}
		f.docTypes = []string{"teaching_material"}
	}
	result, err := s.runSearch(r.Context(), f, req.Query, topK, rerank)
	if err != nil {
		if _, ok := err.(*aiError); ok {
			platform.Error(w, 503, "SEARCH_UNAVAILABLE", "Search is unavailable right now; retry later.")
			return
		}
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) runSearch(ctx context.Context, f filter, query string, topK int, rerank bool) (searchResponse, error) {
	started := time.Now()
	// The keyword leg needs no vector, so it runs while the query is embedded.
	type legResult struct {
		rows []hit
		err  error
	}
	keyword := make(chan legResult, 1)
	go func() {
		rows, err := s.keywordLeg(ctx, f, query)
		keyword <- legResult{rows, err}
	}()
	vector, model, err := s.embedQuery(ctx, query)
	if err != nil {
		<-keyword
		return searchResponse{}, err
	}
	vectorRows, err := s.vectorLeg(ctx, f, vector, model)
	k := <-keyword
	if err == nil {
		err = k.err
	}
	if err != nil {
		return searchResponse{}, err
	}
	hits := distinct(fuse(vectorRows, k.rows))
	fallback := false
	if rerank && len(hits) > 0 {
		fallback = !s.rerankHits(ctx, query, hits)
	}
	if len(hits) > topK {
		hits = hits[:topK]
	}
	if err = s.attachRelated(ctx, f, hits); err != nil {
		return searchResponse{}, err
	}
	slog.Info("search", "vector_hits", len(vectorRows), "keyword_hits", len(k.rows), "returned", len(hits), "rerank", rerank, "rerank_fallback", fallback, "duration_ms", time.Since(started).Milliseconds())
	return searchResponse{hits, fallback}, nil
}

// queryCache keeps recent query vectors, so a repeated question costs no embedding call.
var queryCache = struct {
	sync.Mutex
	entries map[string]cachedVector
}{entries: map[string]cachedVector{}}

type cachedVector struct {
	vector []float32
	model  string
}

func (s server) embedQuery(ctx context.Context, query string) ([]float32, string, error) {
	queryCache.Lock()
	cached, ok := queryCache.entries[query]
	queryCache.Unlock()
	if ok {
		return cached.vector, cached.model, nil
	}
	ctx, cancel := context.WithTimeout(ctx, embedTimeout)
	defer cancel()
	vector, model, err := s.ai.embed(ctx, query)
	if err != nil {
		return nil, "", err
	}
	queryCache.Lock()
	if len(queryCache.entries) >= 256 {
		clear(queryCache.entries)
	}
	queryCache.entries[query] = cachedVector{vector, model}
	queryCache.Unlock()
	return vector, model, nil
}

const legColumns = "id,document_id,doc_type,section_path,page_start,page_end,content,kind,source,image,cites"
const resultColumns = "r.id,r.document_id,(SELECT title FROM documents d WHERE d.id=r.document_id),r.doc_type,r.section_path,r.page_start,r.page_end,r.content,r.kind,r.source,r.image,r.cites"

// vectorLeg compares only vectors from the query's embedding model. relaxed_order lets the filtered HNSW scan
// continue until enough rows pass the filter; the outer ORDER BY makes the ranks exact.
func (s server) vectorLeg(ctx context.Context, f filter, vector []float32, model string) ([]hit, error) {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT set_config('hnsw.iterative_scan','relaxed_order',true), set_config('hnsw.ef_search',$1,true)", fmt.Sprint(efSearch)); err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, fmt.Sprintf("WITH r AS MATERIALIZED (SELECT %s, embedding <=> CAST($5::text AS vector) AS distance FROM document_chunks WHERE %s AND embedding_model=$6 ORDER BY distance LIMIT %d) SELECT %s FROM r ORDER BY r.distance, r.id", legColumns, filterSQL, legLimit, resultColumns), f.args(vectorText(vector), model)...)
	if err != nil {
		return nil, err
	}
	return scanHits(rows)
}

func (s server) keywordLeg(ctx context.Context, f filter, query string) ([]hit, error) {
	rows, err := s.DB.Query(ctx, fmt.Sprintf("WITH r AS MATERIALIZED (SELECT %s, ts_rank_cd(tsv, q) AS rank FROM document_chunks, websearch_to_tsquery('english', $5) AS q WHERE tsv @@ q AND %s ORDER BY rank DESC, id LIMIT %d) SELECT %s FROM r ORDER BY r.rank DESC, r.id", legColumns, filterSQL, legLimit, resultColumns), f.args(query)...)
	if err != nil {
		return nil, err
	}
	return scanHits(rows)
}

func scanHits(rows pgx.Rows) ([]hit, error) {
	defer rows.Close()
	out := []hit{}
	for rows.Next() {
		var h hit
		if err := rows.Scan(&h.ChunkID, &h.DocumentID, &h.Title, &h.DocType, &h.SectionPath, &h.PageStart, &h.PageEnd, &h.Content, &h.Kind, &h.Source, &h.Image, &h.cites); err != nil {
			return nil, err
		}
		h.Related = []related{}
		out = append(out, h)
	}
	return out, rows.Err()
}

// fuse merges the legs with reciprocal rank fusion, Σ 1/(rrfK + rank). Ties go to the better single-leg rank,
// then the chunk id, so the order is deterministic.
func fuse(vector, keyword []hit) []hit {
	byID := map[string]*hit{}
	var order []string
	add := func(rows []hit, rank func(*hit, int)) {
		for i, row := range rows {
			h, ok := byID[row.ChunkID]
			if !ok {
				copy := row
				h = &copy
				byID[row.ChunkID] = h
				order = append(order, row.ChunkID)
			}
			rank(h, i+1)
			h.rrf += 1 / float64(rrfK+i+1)
		}
	}
	add(vector, func(h *hit, r int) { h.vectorRank = r })
	add(keyword, func(h *hit, r int) { h.keywordRank = r })
	out := make([]hit, 0, len(order))
	for _, id := range order {
		h := *byID[id]
		h.Score = h.rrf
		out = append(out, h)
	}
	best := func(h hit) int {
		if h.vectorRank == 0 || (h.keywordRank != 0 && h.keywordRank < h.vectorRank) {
			return h.keywordRank
		}
		return h.vectorRank
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].rrf != out[j].rrf {
			return out[i].rrf > out[j].rrf
		}
		if best(out[i]) != best(out[j]) {
			return best(out[i]) < best(out[j])
		}
		return out[i].ChunkID < out[j].ChunkID
	})
	if len(out) > rrfKeep {
		out = out[:rrfKeep]
	}
	return out
}

// distinct keeps the best-ranked of hits with the same text: a slide repeated in every deck of a course (its
// schedule, say) would otherwise fill several result slots with one passage.
func distinct(hits []hit) []hit {
	seen := map[string]bool{}
	out := hits[:0:0]
	for _, h := range hits {
		key := strings.Join(strings.Fields(h.Content), " ")
		if !seen[key] {
			seen[key] = true
			out = append(out, h)
		}
	}
	return out
}

// rerankHits reorders hits by ai-core's rerank scores and reports whether it could; on failure the RRF order
// stays.
func (s server) rerankHits(ctx context.Context, query string, hits []hit) bool {
	ctx, cancel := context.WithTimeout(ctx, rerankTimeout)
	defer cancel()
	documents := make([]string, len(hits))
	for i, h := range hits {
		documents[i] = passageText(h.Title, h.SectionPath, h.Content)
	}
	scores, err := s.ai.rerank(ctx, query, documents)
	if err != nil {
		slog.Warn("rerank_fallback", "error", err.Error())
		return false
	}
	for i := range hits {
		hits[i].Score = scores[i]
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].Score > hits[j].Score })
	return true
}

// passageText is what the reranker scores: the title, the chunk's deepest heading and its content, as ai-core's
// ingest.passage_text builds it.
func passageText(title string, section *string, content string) string {
	head := title
	if section != nil && *section != "" {
		parts := strings.Split(*section, " > ")
		head += "\n" + parts[len(parts)-1]
	}
	return head + "\n\n" + content
}

// attachRelated adds, for each hit that cites labels, the figures and tables of its document with those labels,
// under the same filter. No query runs when no hit cites anything.
func (s server) attachRelated(ctx context.Context, f filter, hits []hit) error {
	var citing []string
	for _, h := range hits {
		if len(h.cites) > 0 {
			citing = append(citing, h.ChunkID)
		}
	}
	if len(citing) == 0 {
		return nil
	}
	rows, err := s.DB.Query(ctx, `WITH src AS (SELECT id, document_id, cites FROM document_chunks WHERE id=ANY($5::uuid[]))
SELECT src.id, c.id, c.kind, c.label, c.page_start, c.image, c.content FROM src
JOIN LATERAL (SELECT id, kind, label, page_start, image, content FROM document_chunks WHERE document_id=src.document_id AND label=ANY(src.cites) AND `+filterSQL+`) c ON true
ORDER BY src.id, c.page_start, c.label`, f.args(citing)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	found := map[string][]related{}
	for rows.Next() {
		var from string
		var rel related
		if err = rows.Scan(&from, &rel.ChunkID, &rel.Kind, &rel.Label, &rel.PageStart, &rel.Image, &rel.Content); err != nil {
			return err
		}
		found[from] = append(found[from], rel)
	}
	if err = rows.Err(); err != nil {
		return err
	}
	for i := range hits {
		if rel, ok := found[hits[i].ChunkID]; ok {
			hits[i].Related = rel
		}
	}
	return nil
}

func contains(values []string, want string) bool {
	for _, v := range values {
		if v == want {
			return true
		}
	}
	return false
}
