package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"learning/platform"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// aiCore calls the private ai-core service as Content's own service account (content-indexer, role ai-client).
// ai-core holds the model provider keys; Content stores what it returns (docs/decisions/0002-ai-integration.md).
type aiCore struct {
	base   string
	tokens *tokenSource
	client *http.Client // no overall timeout: each call's context bounds it
}

func newAICore() *aiCore {
	return &aiCore{
		base:   strings.TrimRight(platform.Required("AI_CORE_URL"), "/"),
		tokens: &tokenSource{url: platform.Required("AI_TOKEN_URL"), id: platform.Required("AI_CLIENT_ID"), secret: platform.Required("AI_CLIENT_SECRET"), client: &http.Client{Timeout: 10 * time.Second}},
		client: &http.Client{},
	}
}

// aiError is a non-200 answer from ai-core, or a failure to reach it (Status 0). Message and ResetsAt are set
// for AI_SPEND_LIMIT: the user's daily AI allowance is used up until ResetsAt.
type aiError struct {
	Status   int
	Code     string
	Message  string
	ResetsAt time.Time
}

func (e *aiError) Error() string { return fmt.Sprintf("ai-core %d %s", e.Status, e.Code) }

// spendLimit reports whether err is ai-core refusing because the billed user's daily allowance is used up.
func spendLimit(err error) (*aiError, bool) {
	e, ok := err.(*aiError)
	if ok && e.Status == 429 && e.Code == "AI_SPEND_LIMIT" {
		return e, true
	}
	return nil, false
}

// billed is whose daily AI allowance pays for a call (docs/decisions/0003-ai-spend.md): the user who searched, or
// the teacher who uploaded the document being indexed.
type billed struct {
	UserID string `json:"user_id"`
	OrgID  string `json:"org_id"`
	Role   string `json:"role"`
}

// caller bills the signed-in user, at the teacher allowance when their token holds the teacher role.
func caller(c platform.Claims) billed {
	role := "student"
	if c.Has("teacher") {
		role = "teacher"
	}
	return billed{c.Subject, c.Org, role}
}

// transient reports whether a later retry may succeed: ai-core or its provider being down, slow, rate limited or
// not configured yet, or our token being refused. Anything else (a 4xx about the request) will fail again.
func transient(err error) bool {
	e, ok := err.(*aiError)
	if !ok {
		return true
	}
	return e.Status == 0 || e.Status == 401 || e.Status == 408 || e.Status == 429 || e.Status >= 500
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

// ingest sends one PDF to ai-core: chunks with embeddings and the figure crops they point at, or a failed outcome
// when the PDF itself cannot be indexed.
func (a *aiCore) ingest(ctx context.Context, title string, pdf io.Reader, size int64, who billed) (ingestResult, error) {
	var out ingestResult
	query := url.Values{"title": {title}, "billed_user": {who.UserID}, "billed_org": {who.OrgID}, "billed_role": {who.Role}}
	err := a.call(ctx, "/v1/ingest?"+query.Encode(), "application/pdf", pdf, size, &out)
	return out, err
}

// embed returns the query's vector and the model that made it.
func (a *aiCore) embed(ctx context.Context, text string, who billed) ([]float32, string, error) {
	var out struct {
		Vectors      [][]float32 `json:"vectors"`
		ModelVersion string      `json:"model_version"`
	}
	body, _ := json.Marshal(map[string]any{"operation": "embed", "input": text, "billed_to": who})
	if err := a.call(ctx, "/v1/inference", "application/json", bytes.NewReader(body), int64(len(body)), &out); err != nil {
		return nil, "", err
	}
	if len(out.Vectors) != 1 || len(out.Vectors[0]) != dimensions {
		return nil, "", &aiError{Status: 502, Code: "UNEXPECTED_EMBEDDING"}
	}
	return out.Vectors[0], out.ModelVersion, nil
}

// rerank scores each document against the query, in the documents' order.
func (a *aiCore) rerank(ctx context.Context, query string, documents []string, who billed) ([]float64, error) {
	var out struct {
		Scores []float64 `json:"scores"`
	}
	body, _ := json.Marshal(map[string]any{"query": query, "documents": documents, "billed_to": who})
	if err := a.call(ctx, "/v1/rerank", "application/json", bytes.NewReader(body), int64(len(body)), &out); err != nil {
		return nil, err
	}
	if len(out.Scores) != len(documents) {
		return nil, &aiError{Status: 502, Code: "UNEXPECTED_SCORES"}
	}
	return out.Scores, nil
}

// call POSTs to ai-core with the service token. A refused token is dropped from the cache, so the next call (a
// retry of a transient 401) fetches a new one.
func (a *aiCore) call(ctx context.Context, path, contentType string, body io.Reader, size int64, out any) error {
	token, err := a.tokens.get(ctx)
	if err != nil {
		return &aiError{Code: "TOKEN_UNAVAILABLE"}
	}
	req, err := http.NewRequestWithContext(ctx, "POST", a.base+path, body)
	if err != nil {
		return err
	}
	req.ContentLength = size
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("X-Correlation-ID", platform.Correlation(ctx))
	res, err := a.client.Do(req)
	if err != nil {
		return &aiError{Code: "UNREACHABLE"}
	}
	defer res.Body.Close()
	if res.StatusCode != 200 {
		var e struct {
			Error struct {
				Code     string    `json:"code"`
				Message  string    `json:"message"`
				ResetsAt time.Time `json:"resets_at"`
			} `json:"error"`
		}
		_ = json.NewDecoder(io.LimitReader(res.Body, 1<<16)).Decode(&e)
		if res.StatusCode == 401 {
			a.tokens.reset()
		}
		return &aiError{res.StatusCode, e.Error.Code, e.Error.Message, e.Error.ResetsAt}
	}
	if err = json.NewDecoder(res.Body).Decode(out); err != nil {
		return &aiError{Status: 502, Code: "UNREADABLE_RESPONSE"}
	}
	return nil
}

// tokenSource caches a client-credentials access token until shortly before it expires.
type tokenSource struct {
	url, id, secret string
	client          *http.Client
	mu              sync.Mutex
	token           string
	expires         time.Time
}

func (t *tokenSource) get(ctx context.Context) (string, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.token != "" && time.Now().Before(t.expires) {
		return t.token, nil
	}
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {t.id}, "client_secret": {t.secret}}
	req, err := http.NewRequestWithContext(ctx, "POST", t.url, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	res, err := t.client.Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	var body struct {
		AccessToken string `json:"access_token"`
		ExpiresIn   int    `json:"expires_in"`
	}
	if res.StatusCode != 200 || json.NewDecoder(res.Body).Decode(&body) != nil || body.AccessToken == "" {
		return "", fmt.Errorf("token endpoint answered %d", res.StatusCode)
	}
	t.token, t.expires = body.AccessToken, time.Now().Add(time.Duration(body.ExpiresIn)*time.Second-30*time.Second)
	return t.token, nil
}

func (t *tokenSource) reset() {
	t.mu.Lock()
	t.token = ""
	t.mu.Unlock()
}
