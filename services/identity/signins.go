package main

import (
	"context"
	"errors"
	"fmt"
	"learning/platform"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// signinEvery is how often Identity looks for deleted accounts whose sign-in account is still in Keycloak.
const signinEvery = 15 * time.Second

// keycloakUsers removes sign-in accounts from Keycloak's admin API, as the identity-accounts service account, whose
// only role is realm-management's manage-users.
type keycloakUsers struct {
	admin  string // the realm's admin API, such as http://keycloak:8080/admin/realms/learning
	tokens *platform.ServiceToken
	client *http.Client
}

func newKeycloakUsers() keycloakUsers {
	base, realm := strings.TrimRight(platform.Required("KEYCLOAK_URL"), "/"), platform.Env("KEYCLOAK_REALM", "learning")
	client := &http.Client{Timeout: 10 * time.Second}
	return keycloakUsers{
		admin:  base + "/admin/realms/" + url.PathEscape(realm),
		tokens: &platform.ServiceToken{URL: base + "/realms/" + url.PathEscape(realm) + "/protocol/openid-connect/token", ID: platform.Env("ACCOUNTS_CLIENT_ID", "identity-accounts"), Secret: platform.Required("ACCOUNTS_CLIENT_SECRET"), Client: client},
		client: client,
	}
}

// remove deletes the user's sign-in account. One Keycloak no longer has counts as removed.
func (k keycloakUsers) remove(ctx context.Context, user string) error {
	token, err := k.tokens.Get(ctx)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, k.admin+"/users/"+url.PathEscape(user), nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	res, err := k.client.Do(req)
	if err != nil {
		return err
	}
	res.Body.Close()
	switch res.StatusCode {
	case http.StatusNoContent, http.StatusNotFound:
		return nil
	case http.StatusUnauthorized:
		k.tokens.Reset()
	}
	return fmt.Errorf("keycloak answered %d", res.StatusCode)
}

// signinRemover is what signInCleanup needs from Keycloak.
type signinRemover interface {
	remove(ctx context.Context, user string) error
}

// signInCleanup removes the sign-in account of every deleted account (deleteAccount) from Keycloak, so its user cannot
// sign in again. Identity already refuses the account the moment it is deleted; this retries until Keycloak agrees.
type signInCleanup struct {
	db       *pgxpool.Pool
	keycloak signinRemover
}

func (s signInCleanup) run(ctx context.Context) {
	ticker := time.NewTicker(signinEvery)
	defer ticker.Stop()
	for {
		if removed, err := s.removePending(ctx); err != nil {
			slog.Error("signin_removal_failed", "removed", removed, "error", err.Error())
		} else if removed > 0 {
			slog.Info("signin_accounts_removed", "removed", removed)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// maxSigninBackoff is the longest a failing sign-in removal waits before it is tried again.
const maxSigninBackoff = time.Hour

// signinBackoff is the wait after the nth failed removal of one account: 30 s, 1, 2, 4 minutes and so on, at most
// maxSigninBackoff.
func signinBackoff(failures int) time.Duration {
	return min(signinEvery<<max(1, min(failures, 10)), maxSigninBackoff)
}

// removePending removes up to 20 sign-in accounts that are due and marks each one removed, reporting how many it
// removed. Due ones never tried come first, then the longest waiting. One that fails does not stop the others: it is
// put off by signinBackoff, so accounts Keycloak keeps refusing never fill every batch and hold back the rest. The
// error says how many failed.
func (s signInCleanup) removePending(ctx context.Context) (int, error) {
	rows, err := s.db.Query(ctx, `SELECT id,signin_attempts FROM users WHERE deleted_at IS NOT NULL AND signin_removed_at IS NULL
		AND (signin_next_attempt IS NULL OR signin_next_attempt<=now()) ORDER BY signin_next_attempt NULLS FIRST,deleted_at LIMIT 20`)
	if err != nil {
		return 0, err
	}
	type pending struct {
		id       string
		attempts int
	}
	due, err := pgx.CollectRows(rows, func(row pgx.CollectableRow) (pending, error) {
		var p pending
		return p, row.Scan(&p.id, &p.attempts)
	})
	if err != nil {
		return 0, err
	}
	removed, failures := 0, []error{}
	for _, p := range due {
		if err = s.keycloak.remove(ctx, p.id); err == nil {
			_, err = s.db.Exec(ctx, "UPDATE users SET signin_removed_at=now() WHERE id=$1", p.id)
		}
		if err != nil {
			wait := signinBackoff(p.attempts + 1)
			slog.Warn("signin_removal_retry_later", "attempts", p.attempts+1, "retry_in", wait.String(), "error", err.Error())
			_, putOff := s.db.Exec(ctx, "UPDATE users SET signin_attempts=signin_attempts+1,signin_next_attempt=now()+make_interval(secs=>$2) WHERE id=$1", p.id, wait.Seconds())
			failures = append(failures, errors.Join(err, putOff))
			continue
		}
		removed++
	}
	if len(failures) > 0 {
		return removed, fmt.Errorf("%d of %d sign-in removals failed: %w", len(failures), len(due), errors.Join(failures...))
	}
	return removed, nil
}
