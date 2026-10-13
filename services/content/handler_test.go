package main

import (
	"context"
	"encoding/json"
	"errors"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
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
		return Document{}, nil, pgx.ErrNoRows
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
		{"teacher, rubric", platform.RelationTeacher, rubricID, []string{"teacher"}, 200},
		{"student, teaching material", platform.RelationStudent, teachingID, []string{"student"}, 200},
		{"student, rubric", platform.RelationStudent, rubricID, []string{"student"}, 404},
		// The token's teacher role is not enough: enrolled here, the account sees what students see.
		{"teacher enrolled as a student, rubric", platform.RelationStudent, rubricID, []string{"teacher", "student"}, 404},
		{"no access", "", teachingID, []string{"student"}, 403},
		{"unknown document", platform.RelationTeacher, classID, []string{"teacher"}, 404},
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
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &docs) != nil || teacher != wantTeacher {
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
	} {
		if w := platformtest.Serve(s.routes, http.MethodPost, "/v1/search", body, "user", "teacher"); w.Code != 422 {
			t.Errorf("%s: %d", body, w.Code)
		}
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

// fileStore records what was put and what was removed.
type fileStore struct {
	documentFiles
	puts, removed []string
}

func (f *fileStore) put(_ context.Context, key string, _ []byte, _ string) error {
	f.puts = append(f.puts, key)
	return nil
}

func (f *fileStore) removeOrphan(_, key string) { f.removed = append(f.removed, key) }

func upload(t *testing.T, store uploadStore, files *fileStore, query string) *httptest.ResponseRecorder {
	t.Helper()
	s := server{store: store, access: platformtest.Access(platform.RelationTeacher), files: files}
	mux := http.NewServeMux()
	s.routes(mux)
	var c platform.Claims
	c.Subject, c.Org, c.Realm.Roles = "teacher-1", "org", []string{"teacher"}
	r := httptest.NewRequestWithContext(platform.WithClaims(context.Background(), c), http.MethodPost, "/v1/documents?classroom_id="+classID+query, strings.NewReader("Photosynthesis makes glucose."))
	r.Header.Set("Content-Type", mimeText)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, r)
	return w
}

func TestUploadStoresTheDocumentAndItsOriginal(t *testing.T) {
	var inserted newDocument
	files := &fileStore{}
	w := upload(t, uploadStore{inserted: &inserted, commitAttempted: true}, files, "&title=Notes")
	if w.Code != 201 || inserted.Title != "Notes" || inserted.DocType != docTeachingMaterial || inserted.Status != statusExtracted {
		t.Fatalf("%d %+v", w.Code, inserted.Document)
	}
	if len(files.puts) != 1 || files.puts[0] != inserted.Key || inserted.Org != "org" || inserted.Owner != "teacher-1" {
		t.Fatalf("original stored at %v, row says %q", files.puts, inserted.Key)
	}
}

func TestUploadRemovesAnOriginalWhoseRowWasNeverCommitted(t *testing.T) {
	var inserted newDocument
	failed := &fileStore{}
	if w := upload(t, uploadStore{inserted: &inserted, err: errors.New("db down")}, failed, "&title=Notes"); w.Code != 500 || len(failed.removed) != 1 {
		t.Fatalf("%d, removed %v", w.Code, failed.removed)
	}
	// After an uncertain commit the row may exist, so the original stays.
	uncertain := &fileStore{}
	if w := upload(t, uploadStore{inserted: &inserted, err: errors.New("commit lost"), commitAttempted: true}, uncertain, "&title=Notes"); w.Code != 500 || len(uncertain.removed) != 0 {
		t.Fatalf("%d, removed %v", w.Code, uncertain.removed)
	}
}

func TestUploadValidatesItsFields(t *testing.T) {
	for _, query := range []string{"&title=", "&title=Notes&doc_type=lesson"} {
		if w := upload(t, uploadStore{inserted: &newDocument{}}, &fileStore{}, query); w.Code != 422 {
			t.Errorf("%s: %d", query, w.Code)
		}
	}
}
