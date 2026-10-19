package main

import (
	"context"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is an organization's curriculum topics and their prerequisites.
type store interface {
	countTopics(ctx context.Context, org string, archived bool) (int, error)
	listTopics(ctx context.Context, org string, archived bool, paging ...platform.ListWindow) ([]Topic, error)
	findTopic(ctx context.Context, org, id string) (Topic, error)
	insertTopic(ctx context.Context, id, org, name string) error
	renameTopic(ctx context.Context, org, id, name string) (bool, error)
	archiveTopic(ctx context.Context, org, id string) (bool, error)
	restoreTopic(ctx context.Context, org, id string) (Topic, error)
	insertPrerequisite(ctx context.Context, org, id, prerequisite string) (prerequisiteResult, error)
	listPrerequisites(ctx context.Context, org, id string) ([]Topic, error)
	deletePrerequisite(ctx context.Context, org, id, prerequisite string) (bool, error)
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

// listTopics is the organization's active topics, or its archived ones, by name; up to 200.
func (p pgStore) listTopics(ctx context.Context, org string, archived bool, paging ...platform.ListWindow) ([]Topic, error) {
	window := platform.Window(paging, 200)
	rows, err := p.db.Query(ctx, "SELECT id,name FROM topics WHERE org_id=$1 AND archived=$2 ORDER BY name,id OFFSET $3 LIMIT $4", org, archived, window.Offset, window.Limit)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Topic, error) { return scanTopic(row) })
}

// scanTopic reads the columns id,name.
func scanTopic(row pgx.Row) (Topic, error) {
	var v Topic
	err := row.Scan(&v.ID, &v.Name)
	return v, err
}

func (p pgStore) findTopic(ctx context.Context, org, id string) (Topic, error) {
	return scanTopic(p.db.QueryRow(ctx, "SELECT id,name FROM topics WHERE id=$1 AND org_id=$2 AND NOT archived", id, org))
}

func (p pgStore) insertTopic(ctx context.Context, id, org, name string) error {
	_, err := p.db.Exec(ctx, "INSERT INTO topics(id,org_id,name) VALUES($1,$2,$3)", id, org, name)
	return err
}

// renameTopic reports whether an active topic was updated.
func (p pgStore) renameTopic(ctx context.Context, org, id, name string) (bool, error) {
	res, err := p.db.Exec(ctx, "UPDATE topics SET name=$1 WHERE id=$2 AND org_id=$3 AND NOT archived", name, id, org)
	if err != nil {
		return false, err
	}
	return res.RowsAffected() != 0, nil
}

// archiveTopic reports whether an active topic in the organization was archived.
func (p pgStore) archiveTopic(ctx context.Context, org, id string) (bool, error) {
	res, err := p.db.Exec(ctx, "UPDATE topics SET archived=true WHERE id=$1 AND org_id=$2 AND NOT archived", id, org)
	if err != nil {
		return false, err
	}
	return res.RowsAffected() != 0, nil
}

// restoreTopic makes an archived topic in the organization active again; pgx.ErrNoRows when there is none.
func (p pgStore) restoreTopic(ctx context.Context, org, id string) (Topic, error) {
	return scanTopic(p.db.QueryRow(ctx,
		"UPDATE topics SET archived=false WHERE id=$1 AND org_id=$2 AND archived RETURNING id,name", id, org))
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
	err = tx.QueryRow(ctx, "SELECT count(*) FROM topics WHERE id=ANY($1::uuid[]) AND org_id=$2 AND NOT archived", []string{id, prerequisite}, org).Scan(&count)
	if err != nil {
		return prerequisiteAdded, err
	}
	if count != 2 {
		return prerequisiteInvalid, nil
	}
	var cycle bool
	err = tx.QueryRow(ctx, `WITH RECURSIVE dependencies(id) AS (SELECT prerequisite_id FROM prerequisites WHERE topic_id=$1 UNION SELECT p.prerequisite_id FROM prerequisites p JOIN dependencies d ON p.topic_id=d.id) SELECT EXISTS(SELECT 1 FROM dependencies WHERE id=$2)`, prerequisite, id).Scan(&cycle)
	if err != nil {
		return prerequisiteAdded, err
	}
	if cycle {
		return prerequisiteCycle, nil
	}
	_, err = tx.Exec(ctx, "INSERT INTO prerequisites(topic_id,prerequisite_id) VALUES($1,$2) ON CONFLICT DO NOTHING", id, prerequisite)
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		return prerequisiteAdded, err
	}
	return prerequisiteAdded, nil
}

// listPrerequisites is the topic's active prerequisites in the organization, by name; up to 200.
func (p pgStore) listPrerequisites(ctx context.Context, org, id string) ([]Topic, error) {
	rows, err := p.db.Query(ctx, "SELECT t.id,t.name FROM prerequisites p JOIN topics t ON t.id=p.prerequisite_id WHERE p.topic_id=$1 AND t.org_id=$2 AND NOT t.archived ORDER BY t.name,t.id LIMIT 200", id, org)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (Topic, error) { return scanTopic(row) })
}

// deletePrerequisite reports whether the organization's topic had that prerequisite.
func (p pgStore) deletePrerequisite(ctx context.Context, org, id, prerequisite string) (bool, error) {
	tag, err := p.db.Exec(ctx, "DELETE FROM prerequisites p USING topics t WHERE p.topic_id=$1 AND p.prerequisite_id=$2 AND t.id=p.topic_id AND t.org_id=$3", id, prerequisite, org)
	return tag.RowsAffected() == 1, err
}

func (p pgStore) countTopics(ctx context.Context, org string, archived bool) (int, error) {
	var total int
	err := p.db.QueryRow(ctx, `SELECT count(*) FROM topics WHERE org_id=$1 AND archived=$2`, org, archived).Scan(&total)
	return total, err
}
