package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"learning/platform"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/google/uuid"
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
	mux.HandleFunc("GET /v1/documents/page", s.list)
	mux.HandleFunc("GET /v1/documents/{id}", s.get)
	mux.HandleFunc("GET /v1/documents/{id}/original", s.original)
	mux.HandleFunc("GET /v1/documents/{id}/figures/{image}", s.figure)
	mux.HandleFunc("POST /v1/documents/{id}/reindex", s.reindex)
	mux.HandleFunc("DELETE /v1/documents/{id}", s.remove)
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
	if !platform.TeacherOf(s.access, w, r, classroom) {
		return
	}
	title, docType, problem := uploadFields(r)
	if problem != "" {
		platform.Invalid(w, problem)
		return
	}
	file, size, sum, err := spool(http.MaxBytesReader(w, r.Body, maxPDF))
	var tooBig *http.MaxBytesError
	var disk *fs.PathError
	switch {
	case errors.As(err, &tooBig):
		platform.Error(w, http.StatusRequestEntityTooLarge, "UPLOAD_TOO_LARGE", tooLarge)
		return
	case errors.As(err, &disk): // the temporary file, not the client
		platform.Fail(w, err)
		return
	case err != nil:
		platform.Error(w, http.StatusBadRequest, "UPLOAD_INCOMPLETE", "The upload did not finish; retry.")
		return
	}
	defer discard(file)
	kind := strings.Split(r.Header.Get("Content-Type"), ";")[0]
	chunks, status, err := extractFile(file, kind)
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
	d := newDocument{
		Document: Document{ID: id, Classroom: classroom, Title: title, Type: kind, Status: status, Revision: 1, SHA: sum, DocType: docType, Chunks: chunks},
		Org:      c.Org,
		Owner:    c.Subject,
		Key:      c.Org + "/" + id + "/1",
	}
	if err = s.files.upload(r.Context(), d.Key, file, size, kind); err != nil {
		platform.Error(w, http.StatusServiceUnavailable, "STORAGE_UNAVAILABLE", "Upload storage is unavailable; retry later.")
		return
	}
	commitAttempted, err := s.store.insertDocument(r.Context(), d)
	if errors.Is(err, errClassroomPurged) {
		// Identity deleted the classroom while the file was uploading.
		s.files.removeOrphan(id, d.Key)
		platform.NotFound(w, "Classroom not found.")
		return
	}
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

// spool copies an upload to a temporary file, hashing it on the way, so a 50 MiB PDF is never held in memory. The
// file is rewound for reading; the caller must discard it.
func spool(body io.Reader) (*os.File, int64, string, error) {
	file, err := os.CreateTemp("", "upload-*")
	if err != nil {
		return nil, 0, "", err
	}
	hash := sha256.New()
	size, err := io.Copy(io.MultiWriter(file, hash), body)
	if err == nil {
		_, err = file.Seek(0, io.SeekStart)
	}
	if err != nil {
		discard(file)
		return nil, 0, "", err
	}
	return file, size, hex.EncodeToString(hash.Sum(nil)), nil
}

func discard(file *os.File) {
	_ = file.Close()
	_ = os.Remove(file.Name())
}

// extractFile reads as much of a spooled upload as extract needs (a PDF's first bytes, or a whole text document
// up to one byte past its limit) and rewinds it for storage.
func extractFile(file *os.File, kind string) ([]Chunk, string, error) {
	limit := int64(maxText + 1)
	if kind == mimePDF {
		limit = int64(len(pdfMagic))
	}
	data, err := io.ReadAll(io.LimitReader(file, limit))
	if err != nil {
		return nil, "", err
	}
	if _, err = file.Seek(0, io.SeekStart); err != nil {
		return nil, "", err
	}
	return extract(data, kind)
}

// uploadFields reads an upload's title and document type from the query string (the body is the file itself).
// problem is set when either is invalid. The type is optional and only teaching_material is accepted.
func uploadFields(r *http.Request) (title, docType, problem string) {
	title = strings.TrimSpace(r.URL.Query().Get("title"))
	if title == "" || utf8.RuneCountInString(title) > maxTitle {
		return "", "", fmt.Sprintf("Title must contain 1–%d characters.", maxTitle)
	}
	docType = r.URL.Query().Get("doc_type")
	if docType == "" {
		docType = docTeachingMaterial
	}
	if docType != docTeachingMaterial {
		return "", "", "Only teaching materials can be uploaded; doc_type must be teaching_material."
	}
	return title, docType, ""
}

func (s server) list(w http.ResponseWriter, r *http.Request) {
	classroom := r.URL.Query().Get("classroom_id")
	relation, ok := s.access.Relation(w, r, classroom, "")
	if !ok {
		return
	}
	var paging []platform.ListWindow
	if r.URL.Path == "/v1/documents/page" {
		offset, limit, valid := platform.Page(w, r, 20, 100)
		if !valid {
			return
		}
		paging = []platform.ListWindow{{Offset: offset, Limit: limit}}
	}
	result, err := s.store.listDocuments(r.Context(), platform.Actor(r).Org, classroom, relation == platform.RelationTeacher, paging...)
	if err != nil {
		platform.Fail(w, err)
		return
	}
	if len(paging) > 0 {
		total, err := s.store.countDocuments(r.Context(), platform.Actor(r).Org, classroom, relation == platform.RelationTeacher)
		if err != nil {
			platform.Fail(w, err)
			return
		}
		platform.JSON(w, http.StatusOK, platform.ListPage[Document]{Items: result, Total: total})
		return
	}
	platform.JSON(w, http.StatusOK, result)
}

// visibleDocument looks a document up and checks the caller may see it in its classroom. A document the caller may
// not see answers 404 like a missing one, so its existence doesn't leak. On failure the response is written.
func (s server) visibleDocument(w http.ResponseWriter, r *http.Request, what string, find func(org string) (Document, error)) (Document, bool) {
	v, ok := lookupDocument(w, r, what, find)
	if !ok {
		return Document{}, false
	}
	relation, ok := s.access.Relation(w, r, v.Classroom, "")
	if !ok {
		return Document{}, false
	}
	if !visible(relation, v.DocType) {
		platform.NotFound(w, what+" not found.")
		return Document{}, false
	}
	return v, true
}

// lookupDocument finds the document the path names in the caller's organization, before any access check. On
// failure the response is written: 404 for a malformed or unknown id.
func lookupDocument(w http.ResponseWriter, r *http.Request, what string, find func(org string) (Document, error)) (Document, bool) {
	notFound := what + " not found."
	if !platform.ID(r.PathValue("id")) {
		platform.NotFound(w, notFound)
		return Document{}, false
	}
	v, err := find(platform.Actor(r).Org)
	if !platform.Found(w, err, notFound) {
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
	// The role check comes first so a student is refused before any lookup; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return
	}
	org := platform.Actor(r).Org
	v, ok := lookupDocument(w, r, "Document", func(org string) (Document, error) {
		doc, _, err := s.store.findDocument(r.Context(), org, r.PathValue("id"))
		return doc, err
	})
	if !ok {
		return
	}
	if !platform.TeacherOf(s.access, w, r, v.Classroom) {
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

// remove deletes a document for good: its row with its chunks, vectors and indexing job, its original and figure
// crops in object storage, and emits content.deleted.v1. An indexing run still under way loses its job and stores
// nothing. Questions that cite the document keep the passages they copied.
func (s server) remove(w http.ResponseWriter, r *http.Request) {
	// The role check comes first so a student is refused before any lookup; TeacherOf decides below.
	if !platform.Teacher(w, r) {
		return
	}
	org := platform.Actor(r).Org
	v, ok := lookupDocument(w, r, "Document", func(org string) (Document, error) {
		doc, _, err := s.store.findDocument(r.Context(), org, r.PathValue("id"))
		return doc, err
	})
	if !ok {
		return
	}
	if !platform.TeacherOf(s.access, w, r, v.Classroom) {
		return
	}
	// Commit the logical deletion and cleanup job even if the caller disconnects.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), removeDeadline)
	defer cancel()
	err := s.store.deleteDocument(ctx, org, v.ID)
	if !platform.Found(w, err, "Document not found.") {
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// send streams a stored object to the caller. A 50 MiB original on a slow link takes longer than the request
// deadline and the server's write timeout, so the stream gets its own: downloadDeadline for both reading the object
// and writing the response. A client that goes away still ends it, by failing the write.
func (s server) send(w http.ResponseWriter, r *http.Request, key, kind, disposition string) {
	ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), downloadDeadline)
	defer cancel()
	// Not every ResponseWriter supports deadlines (test recorders don't); the server's write timeout then applies.
	_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(downloadDeadline))
	object, size, err := s.files.open(ctx, key)
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
