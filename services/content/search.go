package main

import (
	"context"
	"errors"
	"learning/platform"
	"log/slog"
	"net/http"
	"slices"
	"sort"
	"strings"
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
	topK, rerank, problem := req.normalize()
	if problem != "" {
		platform.Invalid(w, problem)
		return
	}
	relation, ok := s.access.Relation(w, r, req.Classroom, "")
	if !ok {
		return
	}
	f := filter{platform.Actor(r).Org, req.Classroom, req.DocTypes, req.DocumentIDs}
	if relation != platform.RelationTeacher {
		if len(f.docTypes) > 0 && !slices.Contains(f.docTypes, docTeachingMaterial) {
			platform.JSON(w, http.StatusOK, searchResponse{Results: []hit{}})
			return
		}
		f.docTypes = []string{docTeachingMaterial}
	}
	result, err := s.runSearch(r.Context(), f, req.Query, topK, rerank, caller(platform.Actor(r)))
	if err != nil {
		searchFailed(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// normalize trims the query and applies the defaults (top 8, reranked). It returns a message when the request is
// out of bounds.
func (req *searchRequest) normalize() (topK int, rerank bool, problem string) {
	req.Query = strings.TrimSpace(req.Query)
	topK, rerank = 8, true
	if req.TopK != nil {
		topK = *req.TopK
	}
	if req.Rerank != nil {
		rerank = *req.Rerank
	}
	if req.Query == "" || utf8.RuneCountInString(req.Query) > maxQuery || topK < 1 || topK > rrfKeep || len(req.DocumentIDs) > 100 || len(req.DocTypes) > len(docTypes) {
		return 0, false, "Send a query of 1–1000 characters, top_k 1–30, at most 100 document ids and at most 3 document types."
	}
	for _, id := range req.DocumentIDs {
		if !platform.ID(id) {
			return 0, false, "Document ids must be UUIDs."
		}
	}
	for _, t := range req.DocTypes {
		if !docTypes[t] {
			return 0, false, "Unknown document type."
		}
	}
	return topK, rerank, ""
}

// searchFailed answers a failed search: the user's AI allowance is used up, ai-core is unavailable, or an
// internal error.
func searchFailed(w http.ResponseWriter, err error) {
	if limit, ok := spendLimit(err); ok {
		message := limit.Message
		if message == "" {
			message = "Your daily AI allowance is used up until " + limit.ResetsAt.UTC().Format(time.RFC3339) + "."
		}
		platform.ErrorWith(w, http.StatusTooManyRequests, limit.Code, message, map[string]any{"resets_at": limit.ResetsAt})
		return
	}
	var unavailable *aiError
	if errors.As(err, &unavailable) {
		platform.Error(w, http.StatusServiceUnavailable, "SEARCH_UNAVAILABLE", "Search is unavailable right now; retry later.")
		return
	}
	platform.Fail(w, err)
}

func (s server) runSearch(ctx context.Context, f filter, query string, topK int, rerank bool, who billed) (searchResponse, error) {
	started := time.Now()
	// The keyword leg needs no vector, so it runs while the query is embedded.
	type legResult struct {
		rows []hit
		err  error
	}
	keyword := make(chan legResult, 1)
	go func() {
		rows, err := s.store.keywordLeg(ctx, f, query)
		keyword <- legResult{rows, err}
	}()
	vector, model, err := s.embedQuery(ctx, query, who)
	if err != nil {
		<-keyword
		return searchResponse{}, err
	}
	vectorRows, err := s.store.vectorLeg(ctx, f, vector, model)
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
		if fallback, err = s.rerankHits(ctx, query, hits, who); err != nil {
			return searchResponse{}, err
		}
	}
	if len(hits) > topK {
		hits = hits[:topK]
	}
	if err = s.store.attachRelated(ctx, f, hits); err != nil {
		return searchResponse{}, err
	}
	slog.Info("search", "vector_hits", len(vectorRows), "keyword_hits", len(k.rows), "returned", len(hits), "rerank", rerank, "rerank_fallback", fallback, "duration_ms", time.Since(started).Milliseconds())
	return searchResponse{hits, fallback}, nil
}

func (s server) embedQuery(ctx context.Context, query string, who billed) ([]float32, string, error) {
	if cached, ok := s.vectors.get(query); ok {
		return cached.vector, cached.model, nil
	}
	ctx, cancel := context.WithTimeout(ctx, embedTimeout)
	defer cancel()
	vector, model, err := s.ai.embed(ctx, query, who)
	if err != nil {
		return nil, "", err
	}
	s.vectors.put(query, cachedVector{vector, model})
	return vector, model, nil
}

const legColumns = "id,document_id,doc_type,section_path,page_start,page_end,content,kind,source,image,cites"

// resultColumns are a leg's hits with their document's title; the legs select from r JOIN documents d.
const resultColumns = "r.id,r.document_id,d.title,r.doc_type,r.section_path,r.page_start,r.page_end,r.content,r.kind,r.source,r.image,r.cites"

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
				first := row
				h = &first
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

// rerankHits reorders hits by ai-core's rerank scores and reports whether it fell back: on failure the RRF order
// stays. A user over their daily AI allowance is an error, not a fallback.
func (s server) rerankHits(ctx context.Context, query string, hits []hit, who billed) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, rerankTimeout)
	defer cancel()
	passages := make([]passage, len(hits))
	for i, h := range hits {
		passages[i] = passage{h.Title, h.SectionPath, h.Content}
	}
	scores, err := s.ai.rerank(ctx, query, passages, who)
	if _, ok := spendLimit(err); ok {
		return false, err
	}
	if err != nil {
		slog.Warn("rerank_fallback", "error", err.Error())
		return true, nil
	}
	for i := range hits {
		hits[i].Score = scores[i]
	}
	sort.SliceStable(hits, func(i, j int) bool { return hits[i].Score > hits[j].Score })
	return false, nil
}
