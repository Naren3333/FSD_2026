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
	ai    *aiCore
}

// visible reports whether a caller with this classroom relation may see a document of this type: students see
// teaching material only. The relation comes from Identity, not the token role, so an account that is a teacher
// elsewhere but enrolled here sees what students see.
func visible(relation, docType string) bool {
	return docType == "teaching_material" || relation == platform.RelationTeacher
}

func (s server) upload(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	classroom := r.URL.Query().Get("classroom_id")
	if !s.TeacherOf(w, r, classroom) {
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
	if err == errTooLarge {
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
	commitAttempted, err = s.insertDocument(r.Context(), id, classroom, platform.Actor(r).Org, platform.Actor(r).Subject, title, kind, key, hash, status, docType, chunks)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	stored = true
	platform.JSON(w, 201, Document{ID: id, Classroom: classroom, Title: title, Type: kind, Status: status, Revision: 1, SHA: hash, DocType: docType, Chunks: chunks})
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	relation, ok := s.Relation(w, r, classroom, "")
	if !ok {
		return
	}
	result, err := s.listDocuments(r.Context(), platform.Actor(r).Org, classroom, relation == platform.RelationTeacher)
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
	relation, ok := s.Relation(w, r, v.Classroom, "")
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
	classroom, key, kind, docType, err := s.findOriginal(r.Context(), platform.Actor(r).Org, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.Relation(w, r, classroom, "")
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
	v, err := s.findIndexed(r.Context(), org, r.PathValue("id"), r.PathValue("image"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Figure not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	relation, ok := s.Relation(w, r, v.Classroom, "")
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
	v, _, err := s.findDocument(r.Context(), org, r.PathValue("id"))
	if err == pgx.ErrNoRows {
		platform.Error(w, 404, "NOT_FOUND", "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.TeacherOf(w, r, v.Classroom) {
		return
	}
	if v.Type != "application/pdf" {
		platform.Error(w, 409, "NOT_INDEXABLE", "Only PDFs are indexed.")
		return
	}
	queued, err := s.queueIndex(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !queued {
		platform.Error(w, 409, "ALREADY_INDEXING", "The document is already being indexed.")
		return
	}
	v, _, err = s.findDocument(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, 202, v)
}

func (s server) send(w http.ResponseWriter, r *http.Request, key, kind, disposition string) {
	object, size, err := s.store.open(r.Context(), key)
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
