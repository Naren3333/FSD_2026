package main

import (
	"context"
	"encoding/json"
	"learning/platform"

	"github.com/jackc/pgx/v5"
)

// insertDocument stores the document row, queues a PDF for indexing and emits the outbox event in one
// transaction. commitAttempted reports whether Commit was called: an uncertain
// commit may have succeeded, so callers must then retain the stored original.
func (s server) insertDocument(ctx context.Context, id, classroom, org, owner, title, kind, key, hash, status, docType string, chunks []Chunk) (commitAttempted bool, err error) {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	encoded, err := json.Marshal(chunks)
	if err != nil {
		return false, err
	}
	_, err = tx.Exec(ctx, "INSERT INTO documents(id,classroom_id,org_id,owner_id,title,mime_type,object_key,sha256,status,doc_type,chunks) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)", id, classroom, org, owner, title, kind, key, hash, status, docType, encoded)
	if err == nil && status == "indexing" {
		_, err = tx.Exec(ctx, "INSERT INTO index_jobs(document_id) VALUES($1)", id)
	}
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

const documentColumns = "id,classroom_id,title,mime_type,status,revision,sha256,doc_type,page_count,chunk_count,embedding_model,indexed_at,failure_reason"

func scanDocument(row pgx.Row, v *Document, more ...any) error {
	return row.Scan(append([]any{&v.ID, &v.Classroom, &v.Title, &v.Type, &v.Status, &v.Revision, &v.SHA, &v.DocType, &v.PageCount, &v.ChunkCount, &v.EmbeddingModel, &v.IndexedAt, &v.FailureReason}, more...)...)
}

// listDocuments lists a classroom's documents; students get teaching material only.
func (s server) listDocuments(ctx context.Context, org, classroom string, teacher bool) ([]Document, error) {
	rows, err := s.DB.Query(ctx, "SELECT "+documentColumns+" FROM documents WHERE classroom_id=$1 AND org_id=$2 AND ($3 OR doc_type='teaching_material') ORDER BY created_at DESC LIMIT 100", classroom, org, teacher)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Document{}
	for rows.Next() {
		var v Document
		if err = scanDocument(rows, &v); err != nil {
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
	err := scanDocument(s.DB.QueryRow(ctx, "SELECT "+documentColumns+",chunks FROM documents WHERE id=$1 AND org_id=$2", id, org), &v, &data)
	return v, data, err
}

func (s server) findOriginal(ctx context.Context, org, id string) (classroom, key, kind, docType string, err error) {
	err = s.DB.QueryRow(ctx, "SELECT classroom_id,object_key,mime_type,doc_type FROM documents WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom, &key, &kind, &docType)
	return classroom, key, kind, docType, err
}

// findIndexed finds the document whose current chunks point at a figure crop.
func (s server) findIndexed(ctx context.Context, org, id, image string) (Document, error) {
	var v Document
	err := s.DB.QueryRow(ctx, "SELECT d.id,d.classroom_id,d.doc_type FROM documents d WHERE d.id=$1 AND d.org_id=$2 AND EXISTS(SELECT 1 FROM document_chunks c WHERE c.document_id=d.id AND c.image=$3)", id, org, image).Scan(&v.ID, &v.Classroom, &v.DocType)
	return v, err
}

// queueIndex marks a PDF as indexing and queues its job, unless it is already indexing. Chunks from an earlier
// index stay searchable until the new result replaces them.
func (s server) queueIndex(ctx context.Context, org, id string) (bool, error) {
	tx, err := s.DB.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, "UPDATE documents SET status='indexing',failure_reason=NULL,failure_detail=NULL WHERE id=$1 AND org_id=$2 AND status<>'indexing' AND mime_type='application/pdf'", id, org)
	if err != nil || tag.RowsAffected() == 0 {
		return false, err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO index_jobs(document_id) VALUES($1) ON CONFLICT(document_id) DO UPDATE SET attempts=0,next_attempt=now(),lease=NULL,last_error=NULL", id); err != nil {
		return false, err
	}
	return true, tx.Commit(ctx)
}
