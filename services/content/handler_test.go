package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const (
	teachingID = "6f1c5d2e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"
	rubricID   = "7a2d6e3f-9b5c-4d4e-8f3a-2b3c4d5e6f70"
	classID    = "8b3e7f40-ac6d-4e5f-9a4b-3c4d5e6f7081"
)

// documentStore serves two documents by id; the embedded interface leaves every other method unimplemented, so a
// test fails loudly if a handler reaches one it shouldn't.
type documentStore struct {
	store
	listedForTeacher *bool
}

func (documentStore) findDocument(_ context.Context, _, id string) (Document, []byte, error) {
	docType := map[string]string{teachingID: "teaching_material", rubricID: "rubric"}[id]
	if docType == "" {
		return Document{}, nil, platform.ErrNotFound
	}
	return Document{ID: id, Classroom: classID, Title: "Doc", DocType: docType, Type: "text/plain"}, []byte("[]"), nil
}

func (d documentStore) listDocuments(_ context.Context, _, _ string, teacher bool) ([]Document, error) {
	*d.listedForTeacher = teacher
	return []Document{}, nil
}

func get(t *testing.T, relation, id string, roles ...string) int {
	t.Helper()
	s := server{store: documentStore{}, access: platformtest.Access(relation)}
	return platformtest.Serve(s.routes, http.MethodGet, "/v1/documents/"+id, "", "user", roles...).Code
}

func TestStudentsCannotSeeRubrics(t *testing.T) {
	cases := []struct {
		name, relation, id string
		roles              []string
		status             int
	}{
		{"teacher, rubric", platform.RelationTeacher, rubricID, []string{"teacher"}, http.StatusOK},
		{"student, teaching material", platform.RelationStudent, teachingID, []string{"student"}, http.StatusOK},
		{"student, rubric", platform.RelationStudent, rubricID, []string{"student"}, http.StatusNotFound},
		// The token's teacher role is not enough: enrolled here, the account sees what students see.
		{"teacher token, student relation, rubric", platform.RelationStudent, rubricID, []string{"teacher"}, http.StatusNotFound},
		{"no access", "", teachingID, []string{"student"}, http.StatusForbidden},
		{"unknown document", platform.RelationTeacher, classID, []string{"teacher"}, http.StatusNotFound},
	}
	for _, tc := range cases {
		if got := get(t, tc.relation, tc.id, tc.roles...); got != tc.status {
			t.Errorf("%s: %d", tc.name, got)
		}
	}
}

func TestTheListIsFilteredByTheClassroomRelation(t *testing.T) {
	for relation, wantTeacher := range map[string]bool{platform.RelationTeacher: true, platform.RelationStudent: false} {
		var teacher bool
		s := server{store: documentStore{listedForTeacher: &teacher}, access: platformtest.Access(relation)}
		w := platformtest.Serve(s.routes, http.MethodGet, "/v1/documents?classroom_id="+classID, "", "user", "teacher")
		var docs []Document
		if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &docs) != nil || teacher != wantTeacher {
			t.Errorf("%s: %d, listed as teacher %v", relation, w.Code, teacher)
		}
	}
}

func TestSearchValidatesBeforeAnyLookup(t *testing.T) {
	s := server{store: documentStore{}, access: platformtest.Access(platform.RelationTeacher)}
	for _, body := range []string{
		`{"classroom_id":"` + classID + `","query":"  "}`,
		`{"classroom_id":"` + classID + `","query":"leaves","top_k":31}`,
		`{"classroom_id":"` + classID + `","query":"leaves","doc_types":["lesson"]}`,
		`{"classroom_id":"` + classID + `","query":"leaves","document_ids":["nope"]}`,
		`{"classroom_id":"` + classID + `","query":"leaves","doc_types":["rubric","rubric","rubric","rubric"]}`,
	} {
		if w := platformtest.Serve(s.routes, http.MethodPost, "/v1/search", body, "user", "teacher"); w.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d", body, w.Code)
		}
	}
}

func TestASpendLimitAlwaysExplainsItself(t *testing.T) {
	w := httptest.NewRecorder()
	searchFailed(w, &aiError{Status: http.StatusTooManyRequests, Code: "AI_SPEND_LIMIT", ResetsAt: time.Date(2026, 10, 18, 0, 0, 0, 0, time.UTC)})
	if w.Code != http.StatusTooManyRequests || !strings.Contains(w.Body.String(), `"message":"Your daily AI allowance is used up until 2026-10-18T00:00:00Z."`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestTooManyAICallsIsItsOwn429(t *testing.T) {
	w := httptest.NewRecorder()
	searchFailed(w, &aiError{Status: http.StatusTooManyRequests, Code: "AI_BUSY"})
	if w.Code != http.StatusTooManyRequests || !strings.Contains(w.Body.String(), `"code":"AI_BUSY"`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

// uploadStore records the inserted document, or fails after or before attempting the commit.
type uploadStore struct {
	store
	inserted        *newDocument
	err             error
	commitAttempted bool
}

func (u uploadStore) insertDocument(_ context.Context, d newDocument) (bool, error) {
	*u.inserted = d
	return u.commitAttempted, u.err
}

// fileStore records what was uploaded and what was removed.
type fileStore struct {
	documentFiles
	puts, removed []string
	bodies        [][]byte
}

func (f *fileStore) upload(_ context.Context, key string, body io.Reader, size int64, _ string) error {
	data, err := io.ReadAll(body)
	if err != nil || int64(len(data)) != size {
		return fmt.Errorf("read %d of %d bytes: %w", len(data), size, err)
	}
	f.puts = append(f.puts, key)
	f.bodies = append(f.bodies, data)
	return nil
}

func (f *fileStore) removeOrphan(_, key string) { f.removed = append(f.removed, key) }

func upload(t *testing.T, store uploadStore, files *fileStore, query string) *httptest.ResponseRecorder {
	t.Helper()
	return uploadBody(t, store, files, query, mimeText, "Photosynthesis makes glucose.")
}

func uploadBody(t *testing.T, store uploadStore, files *fileStore, query, kind, body string) *httptest.ResponseRecorder {
	t.Helper()
	s := server{store: store, access: platformtest.Access(platform.RelationTeacher), files: files}
	r := platformtest.Request(platformtest.Claims("teacher-1", "teacher"), http.MethodPost, "/v1/documents?classroom_id="+classID+query, body)
	r.Header.Set("Content-Type", kind)
	return platformtest.ServeRequest(s.routes, r)
}

func TestUploadStoresTheDocumentAndItsOriginal(t *testing.T) {
	var inserted newDocument
	files := &fileStore{}
	w := upload(t, uploadStore{inserted: &inserted, commitAttempted: true}, files, "&title=Notes")
	if w.Code != http.StatusCreated || inserted.Title != "Notes" || inserted.DocType != docTeachingMaterial || inserted.Status != statusExtracted {
		t.Fatalf("%d %+v", w.Code, inserted.Document)
	}
	if len(files.puts) != 1 || files.puts[0] != inserted.Key || inserted.Org != "org" || inserted.Owner != "teacher-1" {
		t.Fatalf("original stored at %v, row says %q", files.puts, inserted.Key)
	}
}

func TestUploadRemovesAnOriginalWhoseRowWasNeverCommitted(t *testing.T) {
	var inserted newDocument
	failed := &fileStore{}
	if w := upload(t, uploadStore{inserted: &inserted, err: errors.New("db down")}, failed, "&title=Notes"); w.Code != http.StatusInternalServerError || len(failed.removed) != 1 {
		t.Fatalf("%d, removed %v", w.Code, failed.removed)
	}
	// After an uncertain commit the row may exist, so the original stays.
	uncertain := &fileStore{}
	if w := upload(t, uploadStore{inserted: &inserted, err: errors.New("commit lost"), commitAttempted: true}, uncertain, "&title=Notes"); w.Code != http.StatusInternalServerError || len(uncertain.removed) != 0 {
		t.Fatalf("%d, removed %v", w.Code, uncertain.removed)
	}
}

func TestAnUploadIntoAPurgedClassroomIsNotFoundAndLeavesNoOriginal(t *testing.T) {
	var inserted newDocument
	files := &fileStore{}
	if w := upload(t, uploadStore{inserted: &inserted, err: errClassroomPurged}, files, "&title=Notes"); w.Code != http.StatusNotFound || len(files.removed) != 1 {
		t.Fatalf("%d, removed %v", w.Code, files.removed)
	}
}

func TestTitleLimitCountsCharacters(t *testing.T) {
	var inserted newDocument
	if w := upload(t, uploadStore{inserted: &inserted}, &fileStore{}, "&title="+strings.Repeat("%C3%A9", maxTitle)); w.Code != http.StatusCreated {
		t.Fatalf("%d characters: %d %s", maxTitle, w.Code, w.Body.String())
	}
	if w := upload(t, uploadStore{inserted: &inserted}, &fileStore{}, "&title="+strings.Repeat("%C3%A9", maxTitle+1)); w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("%d characters: %d", maxTitle+1, w.Code)
	}
}

func TestUploadValidatesItsFields(t *testing.T) {
	for _, query := range []string{"&title=", "&title=Notes&doc_type=lesson"} {
		if w := upload(t, uploadStore{inserted: &newDocument{}}, &fileStore{}, query); w.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d", query, w.Code)
		}
	}
}

func TestUploadStoresAWholePDFAndItsHash(t *testing.T) {
	var inserted newDocument
	files := &fileStore{}
	pdf := pdfMagic + "1.7\n" + strings.Repeat("x", 6<<20) // larger than the text limit, which PDFs don't share
	w := uploadBody(t, uploadStore{inserted: &inserted, commitAttempted: true}, files, "&title=Notes", mimePDF, pdf)
	sum := sha256.Sum256([]byte(pdf))
	if w.Code != http.StatusCreated || inserted.Status != statusIndexing || inserted.SHA != hex.EncodeToString(sum[:]) {
		t.Fatalf("%d %+v", w.Code, inserted.Document)
	}
	if len(files.bodies) != 1 || string(files.bodies[0]) != pdf {
		t.Fatal("the stored original differs from the upload")
	}
}

func TestUploadRefusesOversizedAndUnsupportedFiles(t *testing.T) {
	cases := []struct {
		kind, body string
		code       int
	}{
		{mimeText, strings.Repeat("a", maxText+1), http.StatusRequestEntityTooLarge},
		{mimePDF, strings.Repeat("a", 64), http.StatusUnprocessableEntity},
		{"image/png", "png", http.StatusUnprocessableEntity},
	}
	for _, c := range cases {
		files := &fileStore{}
		if w := uploadBody(t, uploadStore{inserted: &newDocument{}}, files, "&title=Notes", c.kind, c.body); w.Code != c.code || len(files.puts) != 0 {
			t.Errorf("%s: %d, stored %v", c.kind, w.Code, files.puts)
		}
	}
}

func TestReindexNeedsTheClassroomsTeacherAndAPDF(t *testing.T) {
	cases := []struct {
		name, relation, id string
		roles              []string
		status             int
	}{
		{"malformed id", platform.RelationTeacher, "not-a-uuid", []string{"teacher"}, http.StatusNotFound},
		{"unknown document", platform.RelationTeacher, classID, []string{"teacher"}, http.StatusNotFound},
		{"a student", platform.RelationStudent, teachingID, []string{"student"}, http.StatusForbidden},
		{"teacher token, student relation", platform.RelationStudent, teachingID, []string{"teacher"}, http.StatusForbidden},
		{"not a PDF", platform.RelationTeacher, teachingID, []string{"teacher"}, http.StatusConflict},
	}
	for _, tc := range cases {
		s := server{store: documentStore{}, access: platformtest.Access(tc.relation)}
		if w := platformtest.Serve(s.routes, http.MethodPost, "/v1/documents/"+tc.id+"/reindex", "", "user", tc.roles...); w.Code != tc.status {
			t.Errorf("%s: %d %s", tc.name, w.Code, w.Body.String())
		}
	}
}

// refusingModels embeds fine and answers every rerank with err.
type refusingModels struct{ err error }

func (refusingModels) embed(context.Context, string, billed) ([]float32, string, error) {
	return unit(), "text-embedding-3-small", nil
}

func (m refusingModels) rerank(context.Context, string, []passage, billed) ([]float64, error) {
	return nil, m.err
}

func TestARerankRefusedForTooManyCallsIsAnErrorNotAFallback(t *testing.T) {
	hits := []hit{{Title: "a"}, {Title: "b"}}
	for code, wantErr := range map[string]bool{"AI_BUSY": true, "AI_SPEND_LIMIT": true, "PROVIDER_UNAVAILABLE": false} {
		s := server{ai: refusingModels{&aiError{Status: http.StatusTooManyRequests, Code: code}}}
		fallback, err := s.rerankHits(t.Context(), "q", hits, testBilled)
		if (err != nil) != wantErr || fallback == wantErr {
			t.Fatalf("%s: fallback=%v err=%v", code, fallback, err)
		}
	}
}
