package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"learning/platform"
	"log/slog"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type server struct {
	*platform.App
	store storage
}

func (s server) upload(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	classroom := r.URL.Query().Get("classroom_id")
	if !s.Access(w, r, classroom, "") {
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
	if err = s.store.put(r.Context(), key, data, kind); err != nil {
		platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Upload storage is unavailable; retry later.")
		return
	}
	stored := false
	commitAttempted := false
	defer func() {
		if !stored && !commitAttempted {
			s.store.removeOrphan(id, key)
		}
	}()
	commitAttempted, err = s.insertDocument(r.Context(), id, classroom, platform.Actor(r).Org, platform.Actor(r).Subject, title, kind, key, hash, status, chunks)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	stored = true
	platform.JSON(w, 201, Document{id, classroom, title, kind, status, 1, hash, chunks})
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !s.Access(w, r, classroom, "") {
		return
	}
	result, err := s.listDocuments(r.Context(), platform.Actor(r).Org, classroom)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, result)
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	v, data, err := s.findDocument(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.Access(w, r, v.Classroom, "") {
		return
	}
	if err = json.Unmarshal(data, &v.Chunks); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 200, v)
}

func (s server) original(w http.ResponseWriter, r *http.Request) {
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	classroom, key, kind, err := s.findOriginal(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.Access(w, r, classroom, "") {
		return
	}
	object, size, err := s.store.open(r.Context(), key)
	if err != nil {
		platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Original file is unavailable.")
		return
	}
	defer object.Close()
	w.Header().Set("Content-Type", kind)
	w.Header().Set("Content-Disposition", "attachment")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Length", fmt.Sprint(size))
	if _, err = io.Copy(w, object); err != nil {
		slog.Error("download_interrupted", "document_id", r.PathValue("id"))
	}
}
