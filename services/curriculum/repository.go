package main

import "context"

func (s server) listNodes(ctx context.Context, org string) ([]Node, error) {
	rows, err := s.DB.Query(ctx, "SELECT id,kind,name,parent_id FROM nodes WHERE org_id=$1 AND NOT archived ORDER BY name,id LIMIT 200", org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Node{}
	for rows.Next() {
		var v Node
		if err = rows.Scan(&v.ID, &v.Kind, &v.Name, &v.Parent); err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

func (s server) findNode(ctx context.Context, org, id string) (Node, error) {
	var v Node
	err := s.DB.QueryRow(ctx, "SELECT id,kind,name,parent_id FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", id, org).Scan(&v.ID, &v.Kind, &v.Name, &v.Parent)
	return v, err
}

// findKind returns the kind of an active node in the organization.
func (s server) findKind(ctx context.Context, org, id string) (string, error) {
	var kind string
	err := s.DB.QueryRow(ctx, "SELECT kind FROM nodes WHERE id=$1 AND org_id=$2 AND NOT archived", id, org).Scan(&kind)
	return kind, err
}

func (s server) insertNode(ctx context.Context, id, org, kind, name string, parent *string) error {
	_, err := s.DB.Exec(ctx, "INSERT INTO nodes(id,org_id,kind,name,parent_id) VALUES($1,$2,$3,$4,$5)", id, org, kind, name, parent)
	return err
}

// renameNode reports whether an active node was updated.
func (s server) renameNode(ctx context.Context, org, id, name string) (bool, error) {
	res, err := s.DB.Exec(ctx, "UPDATE nodes SET name=$1 WHERE id=$2 AND org_id=$3 AND NOT archived", name, id, org)
	if err != nil {
		return false, err
	}
	return res.RowsAffected() != 0, nil
}

// archiveNode reports whether the node was archived. It stays false when the
// node is absent, already archived, or still has active children.
func (s server) archiveNode(ctx context.Context, org, id string) (bool, error) {
	res, err := s.DB.Exec(ctx, "UPDATE nodes SET archived=true WHERE id=$1 AND org_id=$2 AND NOT archived AND NOT EXISTS(SELECT 1 FROM nodes child WHERE child.parent_id=$1 AND NOT child.archived)", id, org)
	if err != nil {
		return false, err
	}
	return res.RowsAffected() != 0, nil
}

type prerequisiteResult int

const (
	prerequisiteAdded prerequisiteResult = iota
	prerequisiteInvalid
	prerequisiteCycle
)

// insertPrerequisite runs under a global advisory lock so concurrent inserts
// cannot race past the cycle check.
func (s server) insertPrerequisite(ctx context.Context, org, id, prerequisite string) (prerequisiteResult, error) {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return prerequisiteAdded, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(271828)"); err != nil {
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
