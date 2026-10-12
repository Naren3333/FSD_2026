package main

import (
	"context"
	"encoding/json"
	"learning/platform"
)

// insertDocument stores the document row and emits the outbox event in one
// transaction. commitAttempted reports whether Commit was called: an uncertain
// commit may have succeeded, so callers must then retain the stored original.
func (s server) insertDocument(ctx context.Context, id, classroom, org, owner, title, kind, key, hash, status string, chunks []Chunk) (commitAttempted bool, err error) {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	encoded, err := json.Marshal(chunks)
	if err != nil {
		return false, err
	}
	_, err = tx.Exec(ctx, "INSERT INTO documents(id,classroom_id,org_id,owner_id,title,mime_type,object_key,sha256,status,chunks) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", id, classroom, org, owner, title, kind, key, hash, status, encoded)
	if err == nil {
		err = platform.Emit(ctx, tx, "content.uploaded.v1", id, map[string]any{"document_id": id, "classroom_id": classroom, "org_id": org, "revision": 1, "status": status})
	}
	if err == nil {
		// An uncertain commit may have succeeded. Retain the original for reconciliation.
		commitAttempted = true
		err = tx.Commit(ctx)
	}
	return commitAttempted, err
}

func (s server) listDocuments(ctx context.Context, org, classroom string) ([]Document, error) {
	rows, err := s.DB.Query(ctx, "SELECT id,classroom_id,title,mime_type,status,revision,sha256 FROM documents WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, org)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Document{}
	for rows.Next() {
		var v Document
		if err = rows.Scan(&v.ID, &v.Classroom, &v.Title, &v.Type, &v.Status, &v.Revision, &v.SHA); err != nil {
			return nil, err
		}
		result = append(result, v)
	}
	if rows.Err() != nil {
		return nil, rows.Err()
	}
	return result, nil
}

// findDocument returns the raw chunks JSON separately so the handler can
// decode it only after the access check, as before.
func (s server) findDocument(ctx context.Context, org, id string) (Document, []byte, error) {
	var v Document
	var data []byte
	err := s.DB.QueryRow(ctx, "SELECT id,classroom_id,title,mime_type,status,revision,sha256,chunks FROM documents WHERE id=$1 AND org_id=$2", id, org).Scan(&v.ID, &v.Classroom, &v.Title, &v.Type, &v.Status, &v.Revision, &v.SHA, &data)
	return v, data, err
}

func (s server) findOriginal(ctx context.Context, org, id string) (classroom, key, kind string, err error) {
	err = s.DB.QueryRow(ctx, "SELECT classroom_id,object_key,mime_type FROM documents WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom, &key, &kind)
	return classroom, key, kind, err
}
