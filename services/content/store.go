package main

import (
	"context"
	"encoding/json"
	"fmt"
	"learning/platform"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// store is Content's documents, their index jobs and their indexed chunks.
type store interface {
	insertDocument(ctx context.Context, id, classroom, org, owner, title, kind, key, hash, status, docType string, chunks []Chunk) (commitAttempted bool, err error)
	listDocuments(ctx context.Context, org, classroom string, teacher bool) ([]Document, error)
	findDocument(ctx context.Context, org, id string) (Document, []byte, error)
	findOriginal(ctx context.Context, org, id string) (classroom, key, kind, docType string, err error)
	findIndexed(ctx context.Context, org, id, image string) (Document, error)
	queueIndex(ctx context.Context, org, id string) (bool, error)
	vectorLeg(ctx context.Context, f filter, vector []float32, model string) ([]hit, error)
	keywordLeg(ctx context.Context, f filter, query string) ([]hit, error)
	attachRelated(ctx context.Context, f filter, hits []hit) error
}

// pgStore is the store in the service's own Postgres database.
type pgStore struct{ db *pgxpool.Pool }

// insertDocument stores the document row, queues a PDF for indexing and emits the outbox event in one
// transaction. commitAttempted reports whether Commit was called: an uncertain
// commit may have succeeded, so callers must then retain the stored original.
func (p pgStore) insertDocument(ctx context.Context, id, classroom, org, owner, title, kind, key, hash, status, docType string, chunks []Chunk) (commitAttempted bool, err error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(ctx)
	encoded, err := json.Marshal(chunks)
	if err != nil {
		return false, err
	}
	_, err = tx.Exec(ctx, "INSERT INTO documents(id,classroom_id,org_id,owner_id,title,mime_type,object_key,sha256,status,doc_type,chunks) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)", id, classroom, org, owner, title, kind, key, hash, status, docType, encoded)
	if err == nil && status == statusIndexing {
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
func (p pgStore) listDocuments(ctx context.Context, org, classroom string, teacher bool) ([]Document, error) {
	rows, err := p.db.Query(ctx, "SELECT "+documentColumns+" FROM documents WHERE classroom_id=$1 AND org_id=$2 AND ($3 OR doc_type='teaching_material') ORDER BY created_at DESC LIMIT 100", classroom, org, teacher)
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
func (p pgStore) findDocument(ctx context.Context, org, id string) (Document, []byte, error) {
	var v Document
	var data []byte
	err := scanDocument(p.db.QueryRow(ctx, "SELECT "+documentColumns+",chunks FROM documents WHERE id=$1 AND org_id=$2", id, org), &v, &data)
	return v, data, err
}

func (p pgStore) findOriginal(ctx context.Context, org, id string) (classroom, key, kind, docType string, err error) {
	err = p.db.QueryRow(ctx, "SELECT classroom_id,object_key,mime_type,doc_type FROM documents WHERE id=$1 AND org_id=$2", id, org).Scan(&classroom, &key, &kind, &docType)
	return classroom, key, kind, docType, err
}

// findIndexed finds the document whose current chunks point at a figure crop.
func (p pgStore) findIndexed(ctx context.Context, org, id, image string) (Document, error) {
	var v Document
	err := p.db.QueryRow(ctx, "SELECT d.id,d.classroom_id,d.doc_type FROM documents d WHERE d.id=$1 AND d.org_id=$2 AND EXISTS(SELECT 1 FROM document_chunks c WHERE c.document_id=d.id AND c.image=$3)", id, org, image).Scan(&v.ID, &v.Classroom, &v.DocType)
	return v, err
}

// queueIndex marks a PDF as indexing and queues its job, unless it is already indexing. Chunks from an earlier
// index stay searchable until the new result replaces them.
func (p pgStore) queueIndex(ctx context.Context, org, id string) (bool, error) {
	tx, err := p.db.Begin(ctx)
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

// vectorLeg compares only vectors from the query's embedding model. relaxed_order lets the filtered HNSW scan
// continue until enough rows pass the filter; the outer ORDER BY makes the ranks exact.
func (p pgStore) vectorLeg(ctx context.Context, f filter, vector []float32, model string) ([]hit, error) {
	tx, err := p.db.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "SELECT set_config('hnsw.iterative_scan','relaxed_order',true), set_config('hnsw.ef_search',$1,true)", fmt.Sprint(efSearch)); err != nil {
		return nil, err
	}
	rows, err := tx.Query(ctx, fmt.Sprintf("WITH r AS MATERIALIZED (SELECT %s, embedding <=> CAST($5::text AS vector) AS distance FROM document_chunks WHERE %s AND embedding_model=$6 ORDER BY distance LIMIT %d) SELECT %s FROM r ORDER BY r.distance, r.id", legColumns, filterSQL, legLimit, resultColumns), f.args(vectorText(vector), model)...)
	if err != nil {
		return nil, err
	}
	return scanHits(rows)
}

func (p pgStore) keywordLeg(ctx context.Context, f filter, query string) ([]hit, error) {
	rows, err := p.db.Query(ctx, fmt.Sprintf("WITH r AS MATERIALIZED (SELECT %s, ts_rank_cd(tsv, q) AS rank FROM document_chunks, websearch_to_tsquery('english', $5) AS q WHERE tsv @@ q AND %s ORDER BY rank DESC, id LIMIT %d) SELECT %s FROM r ORDER BY r.rank DESC, r.id", legColumns, filterSQL, legLimit, resultColumns), f.args(query)...)
	if err != nil {
		return nil, err
	}
	return scanHits(rows)
}

// attachRelated adds, for each hit that cites labels, the figures and tables of its document with those labels,
// under the same filter. No query runs when no hit cites anything.
func (p pgStore) attachRelated(ctx context.Context, f filter, hits []hit) error {
	var citing []string
	for _, h := range hits {
		if len(h.cites) > 0 {
			citing = append(citing, h.ChunkID)
		}
	}
	if len(citing) == 0 {
		return nil
	}
	rows, err := p.db.Query(ctx, `WITH src AS (SELECT id, document_id, cites FROM document_chunks WHERE id=ANY($5::uuid[]))
SELECT src.id, c.id, c.kind, c.label, c.page_start, c.image, c.content FROM src
JOIN LATERAL (SELECT id, kind, label, page_start, image, content FROM document_chunks WHERE document_id=src.document_id AND label=ANY(src.cites) AND `+filterSQL+`) c ON true
ORDER BY src.id, c.page_start, c.label`, f.args(citing)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	found := map[string][]related{}
	for rows.Next() {
		var from string
		var rel related
		if err = rows.Scan(&from, &rel.ChunkID, &rel.Kind, &rel.Label, &rel.PageStart, &rel.Image, &rel.Content); err != nil {
			return err
		}
		found[from] = append(found[from], rel)
	}
	if err = rows.Err(); err != nil {
		return err
	}
	for i := range hits {
		if rel, ok := found[hits[i].ChunkID]; ok {
			hits[i].Related = rel
		}
	}
	return nil
}
