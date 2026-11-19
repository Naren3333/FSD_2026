package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"slices"
	"time"
)

// errUsernameTaken is create's answer when keycloak already has an account with that username.
var errUsernameTaken = errors.New("the username is taken")

// accounts creates and removes sign-in accounts for a school's admins.
type accounts interface {
	// create makes a sign-in account in the organization with the realm role and a temporary password,
	// which its user must change at first sign-in, and returns its id and that password.
	create(ctx context.Context, org, role, username, first, last string) (id, password string, err error)
	// resetPassword gives the account a new temporary password, which its user must change at their next
	// sign-in, and returns it; errNoSignIn when keycloak has no such account.
	resetPassword(ctx context.Context, user string) (password string, err error)
	remove(ctx context.Context, user string) error
}

// errNoSignIn is resetPassword's answer when keycloak has no sign-in account for the user.
var errNoSignIn = errors.New("keycloak has no sign-in account for the user")

// resetPassword replaces the account's password with a random temporary one. an admin whose answer was lost
// resets it again, which replaces the one they never saw.
func (k keycloakUsers) resetPassword(ctx context.Context, user string) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, accountWork)
	defer cancel()
	password, err := random128()
	if err != nil {
		return "", err
	}
	credential := map[string]any{"type": "password", "value": password, "temporary": true}
	res, err := k.do(ctx, http.MethodPut, "/users/"+url.PathEscape(user)+"/reset-password", credential)
	if err != nil {
		return "", err
	}
	res.Body.Close()
	switch res.StatusCode {
	case http.StatusNoContent:
		return password, nil
	case http.StatusNotFound:
		return "", errNoSignIn
	}
	return "", fmt.Errorf("keycloak answered %d resetting a password", res.StatusCode)
}

// random128 is a random 128-bit value, url-safe: a temporary password, or the mark of the request making an account.
func random128() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// accountWork bounds making one account, and separately removing a half-made one. both run apart from the
// request, so an admin who leaves midway cannot cancel them between keycloak making the account and removing it.
var accountWork = 30 * time.Second

// requestAttribute names the attribute that marks an account with the request that made it, so a request
// whose answer was lost removes its own account and never another request's.
const requestAttribute = "identity_request"

// create makes the account, gives it the role, and checks keycloak kept its organization; on any failure
// after the account may exist it removes it again, so no half-made account can sign in.
func (k keycloakUsers) create(ctx context.Context, org, role, username, first, last string) (string, string, error) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), accountWork)
	defer cancel()
	password, err := random128()
	if err != nil {
		return "", "", err
	}
	request, err := random128()
	if err != nil {
		return "", "", err
	}
	user := map[string]any{
		"username": username, "enabled": true, "firstName": first, "lastName": last,
		"attributes":  map[string][]string{"org_id": {org}, requestAttribute: {request}},
		"credentials": []map[string]any{{"type": "password", "value": password, "temporary": true}},
	}
	id, err := k.post(ctx, org, username, request, user)
	if err != nil {
		return "", "", err
	}
	if err = k.finish(ctx, id, org, role); err != nil {
		cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), accountWork)
		defer stop()
		if removed := k.remove(cleanup, id); removed != nil {
			err = errors.Join(err, fmt.Errorf("removing the half-made account: %w", removed))
		}
		return "", "", err
	}
	return id, password, nil
}

// post sends the new account, marked with request, and answers its id. when keycloak may have made it
// without saying so (a lost answer, or a server error a proxy may have sent for it) it removes the account
// that request made, unless the request never reached keycloak.
func (k keycloakUsers) post(ctx context.Context, org, username, request string, user map[string]any) (string, error) {
	res, err := k.do(ctx, http.MethodPost, "/users", user)
	if err == nil {
		res.Body.Close()
		if res.StatusCode >= http.StatusInternalServerError {
			err = fmt.Errorf("keycloak answered %d creating an account", res.StatusCode)
		}
	}
	if err != nil {
		if !neverSent(err) {
			// the request may have used up the work's time, so the cleanup has its own.
			cleanup, stop := context.WithTimeout(context.WithoutCancel(ctx), accountWork)
			defer stop()
			if removed := k.removeLost(cleanup, org, username, request); removed != nil {
				err = errors.Join(err, fmt.Errorf("removing an account whose creation went unanswered: %w", removed))
			}
		}
		return "", err
	}
	switch res.StatusCode {
	case http.StatusConflict:
		return "", errUsernameTaken
	case http.StatusCreated:
	default:
		return "", fmt.Errorf("keycloak answered %d creating an account", res.StatusCode)
	}
	location, err := url.Parse(res.Header.Get("Location"))
	id := ""
	if err == nil {
		id = path.Base(location.Path)
	}
	if id == "" || id == "." || id == "/" {
		return "", errors.New("keycloak gave no id for the new account")
	}
	return id, nil
}

// lostChecks are the waits before each look for an account whose creation went unanswered: keycloak may
// still be writing it, so identity looks at once and twice more.
var lostChecks = []time.Duration{0, 2 * time.Second, 3 * time.Second}

// neverSent reports whether the request failed before reaching keycloak, so no account can have been made.
func neverSent(err error) bool {
	var op *net.OpError
	return errors.As(err, &op) && op.Op == "dial"
}

// removeLost removes the organization's account with the username if the request marked request made it;
// an account another request made, or another school's, is left alone.
func (k keycloakUsers) removeLost(ctx context.Context, org, username, request string) error {
	query := url.Values{"username": {username}, "exact": {"true"}}
	for _, wait := range lostChecks {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(wait):
		}
		var found []struct {
			ID         string              `json:"id"`
			Attributes map[string][]string `json:"attributes"`
		}
		if err := k.read(ctx, "/users?"+query.Encode(), &found); err != nil {
			return err
		}
		for _, user := range found {
			if slices.Equal(user.Attributes["org_id"], []string{org}) && slices.Equal(user.Attributes[requestAttribute], []string{request}) {
				return k.remove(ctx, user.ID)
			}
		}
	}
	return nil
}

// finish gives the new account its realm role and checks its organization was kept: a realm whose user
// profile drops unmanaged attributes would leave it without one.
func (k keycloakUsers) finish(ctx context.Context, id, org, role string) error {
	var realmRole map[string]any
	if err := k.read(ctx, "/roles/"+url.PathEscape(role), &realmRole); err != nil {
		return err
	}
	res, err := k.do(ctx, http.MethodPost, "/users/"+url.PathEscape(id)+"/role-mappings/realm", []map[string]any{realmRole})
	if err != nil {
		return err
	}
	res.Body.Close()
	if res.StatusCode != http.StatusNoContent {
		return fmt.Errorf("keycloak answered %d assigning the role", res.StatusCode)
	}
	var created struct {
		Attributes map[string][]string `json:"attributes"`
	}
	if err = k.read(ctx, "/users/"+url.PathEscape(id), &created); err != nil {
		return err
	}
	if !slices.Equal(created.Attributes["org_id"], []string{org}) {
		return errors.New("keycloak did not keep the account's org_id; set the realm's unmanaged attribute policy to ADMIN_EDIT")
	}
	return nil
}

// read gets a json document from the realm's admin api.
func (k keycloakUsers) read(ctx context.Context, path string, into any) error {
	res, err := k.do(ctx, http.MethodGet, path, nil)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("keycloak answered %d reading %s", res.StatusCode, path)
	}
	return json.NewDecoder(io.LimitReader(res.Body, 1<<20)).Decode(into)
}

// do sends one admin api request with the service account's token, dropping a token keycloak rejects.
func (k keycloakUsers) do(ctx context.Context, method, path string, body any) (*http.Response, error) {
	token, err := k.tokens.Get(ctx)
	if err != nil {
		return nil, err
	}
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(ctx, method, k.admin+path, reader)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := k.client.Do(req)
	if err == nil && res.StatusCode == http.StatusUnauthorized {
		k.tokens.Reject(token)
	}
	return res, err
}
