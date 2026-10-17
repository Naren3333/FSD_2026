package main

import (
	"context"
	"errors"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is an organization's curriculum tree and skill prerequisites.
type store interface {
	listNodes(ctx context.Context, org string) ([]Node, error)
	findNode(ctx context.Context, org, id string) (Node, error)
	findKind(ctx context.Context, org, id string) (string, error)
	insertNode(ctx context.Context, id, org, kind, name string, parent *string) error
	renameNode(ctx context.Context, org, id, name string) (bool, error)
	archiveNode(ctx context.Context, org, id string) (archiveResult, error)
	insertPrerequisite(ctx context.Context, org, id, prerequisite string) (prerequisiteResult, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

func (p pgStore) listNodes(ctx context.Context, org string) ([]Node, error) {
	rows, err := p.db.Query(ctx, "SELECT id,kind,name,parent_id FROM nodes WHERE org_id=$1 AND NOT archived ORDER BY name,id LIMIT 200", org)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Node, error) { return scanNode(row) })
}

// scanNode reads the columns id,kind,name,parent_id.
func scanNode(row pgx.Row) (Node, error) {
	var v Node
	err := row.Scan(&v.ID, &v.Kind, &v.Name, &v.Parent)
	return v, err
}

func (p pgStore) findNode(ctx context.Context, org, id string) (Node, error) {
	return scanNode(p.db.QueryRow(ctx, "SELECT id,kind,name,parent_id FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", id, org))
}

// findKind returns the kind of an active node in the organization.
func (p pgStore) findKind(ctx context.Context, org, id string) (string, error) {
	var kind string
	err := p.db.QueryRow(ctx, "SELECT kind FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", id, org).Scan(&kind)
	return kind, err
}

func (p pgStore) insertNode(ctx context.Context, id, org, kind, name string, parent *string) error {
	_, err := p.db.Exec(ctx, "INSERT INTO nodes(id,org_id,kind,name,parent_id) VALUES($1,$2,$3,$4,$5)", id, org, kind, name, parent)
	return err
}

// renameNode reports whether an active node was updated.
func (p pgStore) renameNode(ctx context.Context, org, id, name string) (bool, error) {
	res, err := p.db.Exec(ctx, "UPDATE nodes SET name=$1 WHERE id=$2 AND org_id=$3 AND NOT archived", name, id, org)
	if err != nil {
		return false, err
	}
	return res.RowsAffected() != 0, nil
}

type archiveResult int

const (
	archived archiveResult = iota
	archiveMissing
	archiveHasChildren
)

// archiveNode archives an active node that has no active children. It answers archiveMissing when there is no active
// node with that id in the organization, and archiveHasChildren when it still has active children.
func (p pgStore) archiveNode(ctx context.Context, org, id string) (archiveResult, error) {
	res, err := p.db.Exec(ctx, "UPDATE nodes SET archived=true WHERE id=$1 AND org_id=$2 AND NOT archived AND NOT EXISTS(SELECT 1 FROM nodes child WHERE child.parent_id=$1 AND NOT child.archived)", id, org)
	if err != nil {
		return archived, err
	}
	if res.RowsAffected() != 0 {
		return archived, nil
	}
	if _, err = p.findKind(ctx, org, id); errors.Is(err, pgx.ErrNoRows) {
		return archiveMissing, nil
	}
	if err != nil {
		return archived, err
	}
	return archiveHasChildren, nil
}

type prerequisiteResult int

const (
	prerequisiteAdded prerequisiteResult = iota
	prerequisiteInvalid
	prerequisiteCycle
)

// insertPrerequisite runs under the organization's advisory lock so two concurrent edges cannot together race past
// the cycle check. Organizations don't wait for each other: their graphs never connect.
func (p pgStore) insertPrerequisite(ctx context.Context, org, id, prerequisite string) (prerequisiteResult, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return prerequisiteAdded, err
	}
	defer tx.Rollback(ctx)
	if err = platform.Lock(ctx, tx, platform.LockPrerequisites, org); err != nil {
		return prerequisiteAdded, err
	}
	var count int
	err = tx.QueryRow(ctx, "SELECT count(*) FROM nodes WHERE id=ANY($1::uuid[]) AND org_id=$2 AND kind='skill' AND NOT archived", []string{id, prerequisite}, org).Scan(&count)
	if err != nil {
		return prerequisiteAdded, err
	}
	if count != 2 {
		return prerequisiteInvalid, nil
	}
	var cycle bool
	err = tx.QueryRow(ctx, `WITH RECURSIVE dependencies(id) AS (SELECT prerequisite_id FROM prerequisites WHERE skill_id=$1 UNION SELECT p.prerequisite_id FROM prerequisites p JOIN dependencies d ON p.skill_id=d.id) SELECT EXISTS(SELECT 1 FROM dependencies WHERE id=$2)`, prerequisite, id).Scan(&cycle)
	if err != nil {
		return prerequisiteAdded, err
	}
	if cycle {
		return prerequisiteCycle, nil
	}
	_, err = tx.Exec(ctx, "INSERT INTO prerequisites(skill_id,prerequisite_id) VALUES($1,$2) ON CONFLICT DO NOTHING", id, prerequisite)
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return prerequisiteAdded, err
	}
	return prerequisiteAdded, nil
}
