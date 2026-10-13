package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"learning/platform"
	"log/slog"
	"net/http"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// server answers Content's HTTP routes: documents and chunks in its store, originals and figure crops in object
// storage, classroom access from Identity, and query embeddings and reranking from ai-core.
type server struct {
	store  store
	access platform.ClassroomAccess
	files  documentFiles
	ai     searchModels
}

func (s server) routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/documents", s.upload)
	mux.HandleFunc("GET /v1/documents", s.list)
	mux.HandleFunc("GET /v1/documents/{id}", s.get)
	mux.HandleFunc("GET /v1/documents/{id}/original", s.original)
	mux.HandleFunc("GET /v1/documents/{id}/figures/{image}", s.figure)
	mux.HandleFunc("POST /v1/documents/{id}/reindex", s.reindex)
	mux.HandleFunc("POST /v1/search", s.search)
}

// visible reports whether a caller with this classroom relation may see a document of this type: students see
// teaching material only. The relation comes from Identity, not the token role, so an account that is a teacher
// elsewhere but enrolled here sees what students see.
func visible(relation, docType string) bool {
	return docType == "teaching_material" || relation == platform.RelationTeacher
}

func (s server) upload(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !s.access.TeacherOf(w, r, classroom) {
		return
	}
	title := strings.TrimSpace(r.URL.Query().Get("title"))
	if title == "" || len(title) > 160 {
		platform.Error(w, 422, "VALIDATION_ERROR", "Title must contain 1–160 characters.")
		return
	}
	docType := r.URL.Query().Get("doc_type")
	if docType == "" {
		docType = "teaching_material"
	}
	if !docTypes[docType] {
		platform.Error(w, 422, "VALIDATION_ERROR", "doc_type must be teaching_material, rubric or marking_scheme.")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxPDF)
	data, err := io.ReadAll(r.Body)
	if err != nil {
		platform.Error(w, 413, "UPLOAD_TOO_LARGE", "PDFs must be no larger than 50 MiB and text documents 5 MiB.")
		return
	}
	kind := strings.Split(r.Header.Get("Content-Type"), ";")[0]
	chunks, status, err := extract(data, kind)
	if errors.Is(err, errTooLarge) {
		platform.Error(w, 413, "UPLOAD_TOO_LARGE", "PDFs must be no larger than 50 MiB and text documents 5 MiB.")
		return
	}
	if err != nil {
		platform.Error(w, 422, "UNSUPPORTED_DOCUMENT", err.Error())
		return
	}
	id := uuid.NewString()
	key := platform.Actor(r).Org + "/" + id + "/1"
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	if err = s.files.put(r.Context(), key, data, kind); err != nil {
		platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Upload storage is unavailable; retry later.")
		return
	}
	stored := false
	commitAttempted := false
	defer func() {
		if !stored && !commitAttempted {
			s.files.removeOrphan(id, key)
		}
	}()
	commitAttempted, err = s.store.insertDocument(r.Context(), id, classroom, platform.Actor(r).Org, platform.Actor(r).Subject, title, kind, key, hash, status, docType, chunks)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	stored = true
	platform.JSON(w, 201, Document{ID: id, Classroom: classroom, Title: title, Type: kind, Status: status, Revision: 1, SHA: hash, DocType: docType, Chunks: chunks})
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, classroom, "")
	if !ok {
		return
	}
	result, err := s.store.listDocuments(r.Context(), platform.Actor(r).Org, classroom, relation == platform.RelationTeacher)
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
	v, data, err := s.store.findDocument(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.access.Relation(w, r, v.Classroom, "")
	if !ok {
		return
	}
	if !visible(relation, v.DocType) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
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
	classroom, key, kind, docType, err := s.store.findOriginal(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.access.Relation(w, r, classroom, "")
	if !ok {
		return
	}
	if !visible(relation, docType) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	s.send(w, r, key, kind, "attachment")
}

// figure serves a figure crop that a search result points at.
func (s server) figure(w http.ResponseWriter, r *http.Request) {
	if !platform.ID(r.PathValue("id")) || !figureName.MatchString(r.PathValue("image")) {
		platform.Error(w, 404, "NOT_FOUND", "Figure not found.")
		return
	}
	org := platform.Actor(r).Org
	v, err := s.store.findIndexed(r.Context(), org, r.PathValue("id"), r.PathValue("image"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Figure not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.access.Relation(w, r, v.Classroom, "")
	if !ok {
		return
	}
	if !visible(relation, v.DocType) {
		platform.Error(w, 404, "NOT_FOUND", "Figure not found.")
		return
	}
	s.send(w, r, figureKey(org, v.ID, r.PathValue("image")), "image/png", "inline")
}

// reindex queues a PDF for indexing again: after a failure, or a PDF uploaded before indexing existed.
func (s server) reindex(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	org := platform.Actor(r).Org
	v, _, err := s.store.findDocument(r.Context(), org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.access.TeacherOf(w, r, v.Classroom) {
		return
	}
	if v.Type != "application/pdf" {
		platform.Error(w, 409, "NOT_INDEXABLE", "Only PDFs are indexed.")
		return
	}
	queued, err := s.store.queueIndex(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !queued {
		platform.Error(w, 409, "ALREADY_INDEXING", "The document is already being indexed.")
		return
	}
	v, _, err = s.store.findDocument(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 202, v)
}

func (s server) send(w http.ResponseWriter, r *http.Request, key, kind, disposition string) {
	object, size, err := s.files.open(r.Context(), key)
	if err != nil {
		platform.Error(w, 503, "STORAGE_UNAVAILABLE", "Original file is unavailable.")
		return
	}
	defer object.Close()
	w.Header().Set("Content-Type", kind)
	w.Header().Set("Content-Disposition", disposition)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Length", fmt.Sprint(size))
	if _, err = io.Copy(w, object); err != nil {
		slog.Error("download_interrupted", "document_id", r.PathValue("id"))
	}
}
