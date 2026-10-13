package main

import (
	"context"
	"encoding/json"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
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
