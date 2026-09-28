package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"io"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
	"strings"
)

//go:embed migrations/001_init.sql
var migration string

type Document struct {
	ID        string  `json:"id"`
	Classroom string  `json:"classroom_id"`
	Title     string  `json:"title"`
	Type      string  `json:"mime_type"`
	Status    string  `json:"status"`
	Revision  int     `json:"revision"`
	SHA       string  `json:"sha256"`
	Chunks    []Chunk `json:"chunks,omitempty"`
}

func main() {
	a, err := platform.New("content", migration+platform.EventMigration)
	if err != nil {
		slog.Error("startup_failed", "error", err.Error())
		os.Exit(1)
	}
	client, err := minio.New(platform.Required("S3_ENDPOINT"), &minio.Options{Creds: credentials.NewStaticV4(platform.Required("S3_ACCESS_KEY"), platform.Required("S3_SECRET_KEY"), ""), Secure: platform.Env("S3_SECURE", "true") == "true"})
	if err != nil {
		os.Exit(1)
	}
	bucket := platform.Required("S3_BUCKET")
	a.Checks = append(a.Checks, func(ctx context.Context) error {
		exists, err := client.BucketExists(ctx, bucket)
		if err != nil {
			return err
		}
		if !exists {
			return fmt.Errorf("document bucket missing")
		}
		return nil
	})
	a.Mux.HandleFunc("POST /v1/documents", func(w http.ResponseWriter, r *http.Request) {
		if !platform.Teacher(w, r) {
			return
		}
		classroom := r.URL.Query().Get("classroom_id")
		if !a.Access(w, r, classroom, "") {
			return
		}
		title := strings.TrimSpace(r.URL.Query().Get("title"))
		if title == "" || len(title) > 160 {
			platform.Error(w, 422, "VALIDATION_ERROR", "Title must contain 1–160 characters.")
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 5<<20)
		data, err := io.ReadAll(r.Body)
		if err != nil {
			platform.Error(w, 413, "UPLOAD_TOO_LARGE", "Documents must be no larger than 5 MiB.")
			return
		}
		kind := strings.Split(r.Header.Get("Content-Type"), ";")[0]
		chunks, status, err := extract(data, kind)
		if err != nil {
			platform.Error(w, 422, "UNSUPPORTED_DOCUMENT", err.Error())
			return
		}
		id := uuid.NewString()
		key := platform.Actor(r).Org + "/" + id + "/1"
		sum := sha256.Sum256(data)
		hash := hex.EncodeToString(sum[:])
		_, err = client.PutObject(r.Context(), bucket, key, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{ContentType: kind})
		if err != nil {
			platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Upload storage is unavailable; retry later.")
			return
		}
		stored := false
		commitAttempted := false
		defer func() {
			if !stored && !commitAttempted {
				cleanup, cancel := context.WithTimeout(context.Background(), 5e9)
				defer cancel()
				if err := client.RemoveObject(cleanup, bucket, key, minio.RemoveObjectOptions{}); err != nil {
					slog.Error("orphan_object_cleanup_failed", "document_id", id)
				}
			}
		}()
		tx, err := a.DB.Begin(r.Context())
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer tx.Rollback(r.Context())
		encoded, err := json.Marshal(chunks)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		_, err = tx.Exec(r.Context(), "INSERT INTO documents(id,classroom_id,org_id,owner_id,title,mime_type,object_key,sha256,status,chunks) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", id, classroom, platform.Actor(r).Org, platform.Actor(r).Subject, title, kind, key, hash, status, encoded)
		if err == nil {
			err = platform.Emit(r.Context(), tx, "content.uploaded.v1", id, map[string]any{"document_id": id, "classroom_id": classroom, "org_id": platform.Actor(r).Org, "revision": 1, "status": status})
		}
		if err == nil {
			// An uncertain commit may have succeeded. Retain the original for reconciliation.
			commitAttempted = true
			err = tx.Commit(r.Context())
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		stored = true
		platform.JSON(w, 201, Document{id, classroom, title, kind, status, 1, hash, chunks})
	})
	a.Mux.HandleFunc("GET /v1/documents", func(w http.ResponseWriter, r *http.Request) {
		classroom := r.URL.Query().Get("classroom_id")
		if !a.Access(w, r, classroom, "") {
			return
		}
		rows, err := a.DB.Query(r.Context(), "SELECT id,classroom_id,title,mime_type,status,revision,sha256 FROM documents WHERE classroom_id=$1 AND org_id=$2 ORDER BY created_at DESC LIMIT 100", classroom, platform.Actor(r).Org)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		defer rows.Close()
		result := []Document{}
		for rows.Next() {
			var v Document
			if err = rows.Scan(&v.ID, &v.Classroom, &v.Title, &v.Type, &v.Status, &v.Revision, &v.SHA); err != nil {
				platform.Fail(w, err)
				return
			}
			result = append(result, v)
		}
		if rows.Err() != nil {
			platform.Fail(w, rows.Err())
			return
		}
		platform.JSON(w, 200, result)
	})
	a.Mux.HandleFunc("GET /v1/documents/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !platform.ID(r.PathValue("id")) {
			platform.Error(w, 404, "NOT_FOUND", "Document not found.")
			return
		}
		var v Document
		var data []byte
		err := a.DB.QueryRow(r.Context(), "SELECT id,classroom_id,title,mime_type,status,revision,sha256,chunks FROM documents WHERE id=$1 AND org_id=$2", r.PathValue("id"), platform.Actor(r).Org).Scan(&v.ID, &v.Classroom, &v.Title, &v.Type, &v.Status, &v.Revision, &v.SHA, &data)
		if err == pgx.ErrNoRows {
			platform.Error(w, 404, "NOT_FOUND", "Document not found.")
			return
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if !a.Access(w, r, v.Classroom, "") {
			return
		}
		if err = json.Unmarshal(data, &v.Chunks); err != nil {
			platform.Fail(w, err)
			return
		}
		platform.JSON(w, 200, v)
	})
	a.Mux.HandleFunc("GET /v1/documents/{id}/original", func(w http.ResponseWriter, r *http.Request) {
		if !platform.ID(r.PathValue("id")) {
			platform.Error(w, 404, "NOT_FOUND", "Document not found.")
			return
		}
		var classroom, key, kind string
		err := a.DB.QueryRow(r.Context(), "SELECT classroom_id,object_key,mime_type FROM documents WHERE id=$1 AND org_id=$2", r.PathValue("id"), platform.Actor(r).Org).Scan(&classroom, &key, &kind)
		if err == pgx.ErrNoRows {
			platform.Error(w, 404, "NOT_FOUND", "Document not found.")
			return
		}
		if err != nil {
			platform.Fail(w, err)
			return
		}
		if !a.Access(w, r, classroom, "") {
			return
		}
		object, err := client.GetObject(r.Context(), bucket, key, minio.GetObjectOptions{})
		if err != nil {
			platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Original file is unavailable.")
			return
		}
		defer object.Close()
		info, err := object.Stat()
		if err != nil {
			platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Original file is unavailable.")
			return
		}
		w.Header().Set("Content-Type", kind)
		w.Header().Set("Content-Disposition", "attachment")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Content-Length", fmt.Sprint(info.Size))
		if _, err = io.Copy(w, object); err != nil {
			slog.Error("download_interrupted", "document_id", r.PathValue("id"))
		}
	})
	if err = a.Run(a.Outbox); err != nil {
		os.Exit(1)
	}
}
