package platform

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"path"
	"regexp"
	"sort"
	"strconv"

	"github.com/jackc/pgx/v5"
)

// migration is one numbered sql file, NNN_name.sql, applied once and recorded in schema_migrations.
type Migration struct {
	Version int
	Name    string
	SQL     string
}

var migrationName = regexp.MustCompile(`^(\d{3})_[a-z0-9_]+\.sql$`)

// migrations reads the numbered sql files in fsys's migrations directory in version order.
func Migrations(fsys fs.FS) ([]Migration, error) {
	entries, err := fs.ReadDir(fsys, "migrations")
	if err != nil {
		return nil, err
	}
	out := make([]Migration, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() || path.Ext(e.Name()) != ".sql" {
			continue
		}
		m := migrationName.FindStringSubmatch(e.Name())
		if m == nil {
			return nil, fmt.Errorf("migration %q must be named NNN_name.sql", e.Name())
		}
		version, _ := strconv.Atoi(m[1])
		body, err := fs.ReadFile(fsys, path.Join("migrations", e.Name()))
		if err != nil {
			return nil, err
		}
		out = append(out, Migration{version, e.Name(), string(body)})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Version < out[j].Version })
	for i, m := range out {
		if m.Version != i+1 {
			return nil, fmt.Errorf("migration versions must run 1..n without gaps or duplicates; found %s at position %d", m.Name, i+1)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no migrations found")
	}
	return out, nil
}

// apply pending numbered migrations and, once per text, the bootstrap statements under the transaction lock.
func migrate(ctx context.Context, tx pgx.Tx, migrations []Migration, bootstrap string) error {
	if _, err := tx.Exec(ctx, "CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"); err != nil {
		return err
	}
	for _, m := range migrations {
		var applied bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version=$1)", m.Version).Scan(&applied); err != nil {
			return err
		}
		if applied {
			continue
		}
		if _, err := tx.Exec(ctx, m.SQL); err != nil {
			return fmt.Errorf("migration %s: %w", m.Name, err)
		}
		if _, err := tx.Exec(ctx, "INSERT INTO schema_migrations(version) VALUES($1) ON CONFLICT DO NOTHING", m.Version); err != nil {
			return err
		}
	}
	if bootstrap == "" {
		return nil
	}
	// recorded by its digest, so a start with the same text takes none of the locks its ddl would, and a change
	// to it runs once, at the upgrade that brings it.
	sum := sha256.Sum256([]byte(bootstrap))
	digest := hex.EncodeToString(sum[:])
	if _, err := tx.Exec(ctx, "CREATE TABLE IF NOT EXISTS schema_bootstraps(digest text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"); err != nil {
		return err
	}
	var applied bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM schema_bootstraps WHERE digest=$1)", digest).Scan(&applied); err != nil || applied {
		return err
	}
	if _, err := tx.Exec(ctx, bootstrap); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, "INSERT INTO schema_bootstraps(digest) VALUES($1) ON CONFLICT DO NOTHING", digest)
	return err
}
