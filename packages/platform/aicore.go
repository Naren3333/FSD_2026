package platform

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// aicore calls the private ai-core service as the calling service's own account (role ai-client). ai-core
// holds the model provider keys and records what each call costs.
type AICore struct {
	Base   string
	Tokens *ServiceToken
	Client *http.Client // no overall timeout: each call's context bounds it
}

// newaicore reads ai_core_url and the service account's ai_token_url, ai_client_id and ai_client_secret.
func NewAICore() AICore {
	return AICore{
		Base:   strings.TrimRight(Required("AI_CORE_URL"), "/"),
		Tokens: &ServiceToken{URL: Required("AI_TOKEN_URL"), ID: Required("AI_CLIENT_ID"), Secret: Required("AI_CLIENT_SECRET"), Client: &http.Client{Timeout: 10 * time.Second}},
		Client: &http.Client{},
	}
}

// maxairesponse bounds a successful ai-core answer.
const maxAIResponse = 64 << 20

// billed is whose daily ai allowance pays for a call (adr 0003).
type Billed struct {
	UserID string `json:"user_id"`
	OrgID  string `json:"org_id"`
	Role   string `json:"role"`
}

// aierror is a non-200 answer from ai-core, or a failure to reach it (status 0).
type AIError struct {
	Status   int
	Code     string
	Message  string
	ResetsAt time.Time
}

func (e *AIError) Error() string { return fmt.Sprintf("ai-core %d %s", e.Status, e.Code) }

// aispendlimit reports whether err is ai-core refusing because the billed user's daily allowance is used
// up.
func AISpendLimit(err error) (*AIError, bool) {
	var e *AIError
	if errors.As(err, &e) && e.Status == http.StatusTooManyRequests && e.Code == "AI_SPEND_LIMIT" {
		return e, true
	}
	return nil, false
}

// aibusy reports whether err is ai-core refusing because the billed user already has as many ai calls
// running as it allows.
func AIBusy(err error) bool {
	var e *AIError
	return errors.As(err, &e) && e.Status == http.StatusTooManyRequests && e.Code == "AI_BUSY"
}

// aitransient reports whether a later retry may succeed: ai-core or its provider being down, slow, rate
// limited or not configured yet, or our token being refused.
func AITransient(err error) bool {
	var e *AIError
	if !errors.As(err, &e) {
		return true
	}
	switch e.Status {
	case 0, http.StatusUnauthorized, http.StatusRequestTimeout, http.StatusTooManyRequests:
		return true
	}
	return e.Status >= http.StatusInternalServerError
}

// call posts to ai-core with the service token and decodes a 200 answer into out.
func (a AICore) Call(ctx context.Context, path, contentType string, body io.Reader, size int64, out any) error {
	token, err := a.Tokens.Get(ctx)
	if err != nil {
		return &AIError{Code: "TOKEN_UNAVAILABLE"}
	}
	req, err := Request(ctx, http.MethodPost, a.Base+path, body)
	if err != nil {
		return err
	}
	req.ContentLength = size
	req.Header.Set("Content-Type", contentType)
	req.Header.Set("Authorization", "Bearer "+token)
	res, err := a.Client.Do(req)
	if err != nil {
		return &AIError{Code: "UNREACHABLE"}
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		var e struct {
			Error struct {
				Code     string    `json:"code"`
				Message  string    `json:"message"`
				ResetsAt time.Time `json:"resets_at"`
			} `json:"error"`
		}
		_ = json.NewDecoder(io.LimitReader(res.Body, 1<<16)).Decode(&e)
		if res.StatusCode == http.StatusUnauthorized {
			a.Tokens.Reset()
		}
		return &AIError{res.StatusCode, e.Error.Code, e.Error.Message, e.Error.ResetsAt}
	}
	if err = json.NewDecoder(io.LimitReader(res.Body, maxAIResponse)).Decode(out); err != nil {
		return &AIError{Status: http.StatusBadGateway, Code: "UNREADABLE_RESPONSE"}
	}
	return nil
}
