package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"learning/platform"
	"net/http"
	"net/url"
)

// aicore calls the private ai-core service as content's own service account (content-indexer, role
// ai-client). ai-core holds the model provider keys; content stores what it returns.
type aiCore struct{ platform.AICore }

func newAICore() *aiCore { return &aiCore{platform.NewAICore()} }

// searchmodels is what search needs from ai-core: query embeddings and reranking, billed to the searching
// user.
type searchModels interface {
	embed(ctx context.Context, text string, who billed) ([]float32, string, error)
	rerank(ctx context.Context, query string, passages []passage, who billed) ([]float64, error)
}

// passage is a search hit as the reranker needs it. ai-core builds the scored text from these parts with
// the same rule it used for the embedded text, so content doesn't keep a copy of that rule.
type passage struct {
	Title       string  `json:"title"`
	SectionPath *string `json:"section_path"`
	Content     string  `json:"content"`
	Source      string  `json:"source,omitempty"`
}

// ingester turns a supported document into chunks with embeddings and the figure crops they point at.
type ingester interface {
	ingest(ctx context.Context, title string, kind string, pdf io.Reader, size int64, who billed) (ingestResult, error)
}

// aierror, billed and the checks on ai-core's answers are the platform's (shared with grading's marking).
type (
	aiError = platform.AIError
	billed  = platform.Billed
)

var (
	spendLimit = platform.AISpendLimit
	transient  = platform.AITransient
)

// caller bills the signed-in user, at the teacher allowance when their token holds the teacher role.
func caller(c platform.Claims) billed {
	role := "student"
	if c.Has("teacher") {
		role = "teacher"
	}
	return billed{UserID: c.Subject, OrgID: c.Org, Role: role}
}

type ingestChunk struct {
	Ordinal     int       `json:"ordinal"`
	Content     string    `json:"content"`
	EmbedText   string    `json:"embed_text"`
	SectionPath *string   `json:"section_path"`
	PageStart   int       `json:"page_start"`
	PageEnd     int       `json:"page_end"`
	TokenCount  int       `json:"token_count"`
	Kind        string    `json:"kind"`
	Source      string    `json:"source"`
	Image       *string   `json:"image"`
	Label       *string   `json:"label"`
	Cites       []string  `json:"cites"`
	Embedding   []float32 `json:"embedding"`
}

type ingestFigure struct {
	Image string `json:"image"`
	PNG   []byte `json:"png_base64"` // encoding/json decodes base64 into []byte
}

type ingestResult struct {
	Outcome        string         `json:"outcome"`
	FailureReason  *string        `json:"failure_reason"`
	FailureDetail  *string        `json:"failure_detail"`
	PageCount      *int           `json:"page_count"`
	OCRPageCount   *int           `json:"ocr_page_count"`
	EmbeddingModel *string        `json:"embedding_model"`
	Chunks         []ingestChunk  `json:"chunks"`
	Figures        []ingestFigure `json:"figures"`
}

// ingest sends one supported document to ai-core: chunks with embeddings and the figure crops they point
// at, or a failed outcome when the pdf itself cannot be indexed.
func (a *aiCore) ingest(ctx context.Context, title string, kind string, pdf io.Reader, size int64, who billed) (ingestResult, error) {
	var out ingestResult
	query := url.Values{"title": {title}, "billed_user": {who.UserID}, "billed_org": {who.OrgID}, "billed_role": {who.Role}}
	err := a.Call(ctx, "/v1/ingest?"+query.Encode(), kind, pdf, size, &out)
	return out, err
}

// embed returns the query's vector and the model that made it.
func (a *aiCore) embed(ctx context.Context, text string, who billed) ([]float32, string, error) {
	var out struct {
		Vectors      [][]float32 `json:"vectors"`
		ModelVersion string      `json:"model_version"`
	}
	body, _ := json.Marshal(map[string]any{"operation": "embed", "input": text, "billed_to": who})
	if err := a.Call(ctx, "/v1/inference", "application/json", bytes.NewReader(body), int64(len(body)), &out); err != nil {
		return nil, "", err
	}
	if len(out.Vectors) != 1 || len(out.Vectors[0]) != dimensions {
		return nil, "", &aiError{Status: http.StatusBadGateway, Code: "UNEXPECTED_EMBEDDING"}
	}
	return out.Vectors[0], out.ModelVersion, nil
}

// rerank scores each passage against the query, in the passages' order.
func (a *aiCore) rerank(ctx context.Context, query string, passages []passage, who billed) ([]float64, error) {
	var out struct {
		Scores []float64 `json:"scores"`
	}
	body, _ := json.Marshal(map[string]any{"query": query, "passages": passages, "billed_to": who})
	if err := a.Call(ctx, "/v1/rerank", "application/json", bytes.NewReader(body), int64(len(body)), &out); err != nil {
		return nil, err
	}
	if len(out.Scores) != len(passages) {
		return nil, &aiError{Status: http.StatusBadGateway, Code: "UNEXPECTED_SCORES"}
	}
	return out.Scores, nil
}
