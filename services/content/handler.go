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
	store   store
	access  platform.ClassroomAccess
	files   documentFiles
	ai      searchModels
	vectors *vectorCache
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
	return docType == docTeachingMaterial || relation == platform.RelationTeacher
}

// tooLarge is the upload limit, in words.
var tooLarge = fmt.Sprintf("PDFs must be no larger than %d MiB and text documents %d MiB.", maxPDF>>20, maxText>>20)

func (s server) upload(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	if !s.access.TeacherOf(w, r, classroom) {
		return
	}
	title, docType, problem := uploadFields(r)
	if problem != "" {
		platform.Invalid(w, problem)
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxPDF)
	data, err := io.ReadAll(r.Body)
	if err != nil {
		platform.Error(w, http.StatusRequestEntityTooLarge, "UPLOAD_TOO_LARGE", tooLarge)
		return
	}
	kind := strings.Split(r.Header.Get("Content-Type"), ";")[0]
	chunks, status, err := extract(data, kind)
	if errors.Is(err, errTooLarge) {
		platform.Error(w, http.StatusRequestEntityTooLarge, "UPLOAD_TOO_LARGE", tooLarge)
		return
	}
	if err != nil {
		platform.Error(w, http.StatusUnprocessableEntity, "UNSUPPORTED_DOCUMENT", err.Error())
		return
	}
	c := platform.Actor(r)
	id := uuid.NewString()
	sum := sha256.Sum256(data)
	d := newDocument{
		Document: Document{ID: id, Classroom: classroom, Title: title, Type: kind, Status: status, Revision: 1, SHA: hex.EncodeToString(sum[:]), DocType: docType, Chunks: chunks},
		Org:      c.Org,
		Owner:    c.Subject,
		Key:      c.Org + "/" + id + "/1",
	}
	if err = s.files.put(r.Context(), d.Key, data, kind); err != nil {
		platform.Error(w, http.StatusServiceUnavailable, "STORAGE_UNAVAILABLE", "Upload storage is unavailable; retry later.")
		return
	}
	commitAttempted, err := s.store.insertDocument(r.Context(), d)
	if err != nil {
		// An uncertain commit may have stored the row, so the original stays; otherwise it is an orphan.
		if !commitAttempted {
			s.files.removeOrphan(id, d.Key)
		}
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusCreated, d.Document)
}

// uploadFields reads an upload's title and document type from the query string (the body is the file itself).
// problem is set when either is invalid.
func uploadFields(r *http.Request) (title, docType, problem string) {
	title = strings.TrimSpace(r.URL.Query().Get("title"))
	if title == "" || len(title) > maxTitle {
		return "", "", fmt.Sprintf("Title must contain 1–%d characters.", maxTitle)
	}
	docType = r.URL.Query().Get("doc_type")
	if docType == "" {
		docType = docTeachingMaterial
	}
	if !docTypes[docType] {
		return "", "", "doc_type must be teaching_material, rubric or marking_scheme."
	}
	return title, docType, ""
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
	platform.JSON(w, http.StatusOK, result)
}

// visibleDocument looks a document up and checks the caller may see it in its classroom. A document the caller may
// not see answers 404 like a missing one, so its existence doesn't leak. On failure the response is written.
func (s server) visibleDocument(w http.ResponseWriter, r *http.Request, what string, find func(org string) (Document, error)) (Document, bool) {
	notFound := what + " not found."
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, notFound)
		return Document{}, false
	}
	v, err := find(platform.Actor(r).Org)
	if errors.Is(err, pgx.ErrNoRows) {
		platform.NotFound(w, notFound)
		return Document{}, false
	}
	if err != nil {
		platform.Fail(w, err)
		return Document{}, false
	}
	relation, ok := s.access.Relation(w, r, v.Classroom, "")
	if !ok {
		return Document{}, false
	}
	if !visible(relation, v.DocType) {
		platform.NotFound(w, notFound)
		return Document{}, false
	}
	return v, true
}

func (s server) get(w http.ResponseWriter, r *http.Request) {
	var data []byte
	v, ok := s.visibleDocument(w, r, "Document", func(org string) (doc Document, err error) {
		doc, data, err = s.store.findDocument(r.Context(), org, r.PathValue("id"))
		return doc, err
	})
	if !ok {
		return
	}
	if err := json.Unmarshal(data, &v.Chunks); err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusOK, v)
}

func (s server) original(w http.ResponseWriter, r *http.Request) {
	var stored storedDocument
	v, ok := s.visibleDocument(w, r, "Document", func(org string) (Document, error) {
		var err error
		stored, err = s.store.findOriginal(r.Context(), org, r.PathValue("id"))
		return stored.Document, err
	})
	if ok {
		s.send(w, r, stored.Key, v.Type, "attachment")
	}
}

// figure serves a figure crop that a search result points at.
func (s server) figure(w http.ResponseWriter, r *http.Request) {
	image := r.PathValue("image")
	if !figureName.MatchString(image) {
		platform.NotFound(w, "Figure not found.")
		return
	}
	v, ok := s.visibleDocument(w, r, "Figure", func(org string) (Document, error) {
		return s.store.findIndexed(r.Context(), org, r.PathValue("id"), image)
	})
	if ok {
		s.send(w, r, figureKey(platform.Actor(r).Org, v.ID, image), mimePNG, "inline")
	}
}

// reindex queues a PDF for indexing again: after a failure, or a PDF uploaded before indexing existed.
func (s server) reindex(w http.ResponseWriter, r *http.Request) {
	if !platform.Teacher(w, r) {
		return
	}
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, "Document not found.")
		return
	}
	org := platform.Actor(r).Org
	v, _, err := s.store.findDocument(r.Context(), org, r.PathValue("id"))
	if errors.Is(err, pgx.ErrNoRows) {
		platform.NotFound(w, "Document not found.")
		return
	}
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !s.access.TeacherOf(w, r, v.Classroom) {
		return
	}
	if v.Type != mimePDF {
		platform.Error(w, http.StatusConflict, "NOT_INDEXABLE", "Only PDFs are indexed.")
		return
	}
	queued, err := s.store.queueIndex(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if !queued {
		platform.Error(w, http.StatusConflict, "ALREADY_INDEXING", "The document is already being indexed.")
		return
	}
	v, _, err = s.store.findDocument(r.Context(), org, v.ID)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	platform.JSON(w, http.StatusAccepted, v)
}

func (s server) send(w http.ResponseWriter, r *http.Request, key, kind, disposition string) {
	object, size, err := s.files.open(r.Context(), key)
	if err != nil {
		platform.Error(w, http.StatusServiceUnavailable, "STORAGE_UNAVAILABLE", "Original file is unavailable.")
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
