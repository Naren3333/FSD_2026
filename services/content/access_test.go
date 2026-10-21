package main

import (
	"context"
	"io"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"sync"
	"testing"
)

// searchrecorder is a store and an ai-core that record what a search asked for and find nothing.
type searchRecorder struct {
	store
	mu      sync.Mutex
	filters []filter
	embeds  int
}

func (s *searchRecorder) record(f filter) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.filters = append(s.filters, f)
}

func (s *searchRecorder) vectorLeg(_ context.Context, f filter, _ []float32, _ string) ([]hit, error) {
	s.record(f)
	return []hit{}, nil
}

func (s *searchRecorder) keywordLeg(_ context.Context, f filter, _ string) ([]hit, error) {
	s.record(f)
	return []hit{}, nil
}

func (s *searchRecorder) attachRelated(_ context.Context, f filter, _ []hit) error {
	s.record(f)
	return nil
}

func (s *searchRecorder) embed(context.Context, string, billed) ([]float32, string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.embeds++
	return unit(), "model", nil
}

func (s *searchRecorder) rerank(context.Context, string, []passage, billed) ([]float64, error) {
	panic("nothing to rerank")
}

func searchAs(t *testing.T, relation, docTypes string, roles ...string) (*searchRecorder, *httptest.ResponseRecorder) {
	t.Helper()
	rec := &searchRecorder{}
	s := server{store: rec, ai: rec, access: platformtest.Access(relation)}
	body := `{"classroom_id":"` + classID + `","query":"leaves","doc_types":` + docTypes + `}`
	return rec, platformtest.Serve(s.routes, http.MethodPost, "/v1/search", body, "user", roles...)
}

func TestAStudentsSearchNeverWidensPastTeachingMaterial(t *testing.T) {
	// asking only for rubrics finds nothing, without a lookup or a paid embedding.
	for _, roles := range [][]string{{"student"}, {"teacher"}} { // a teacher elsewhere, enrolled here
		rec, w := searchAs(t, platform.RelationStudent, `["rubric","marking_scheme"]`, roles...)
		if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"results":[]`) || len(rec.filters) != 0 || rec.embeds != 0 {
			t.Fatalf("%v: %d %s, filters %v, embeds %d", roles, w.Code, w.Body.String(), rec.filters, rec.embeds)
		}
	}
	for _, docTypes := range []string{`null`, `[]`, `["rubric","teaching_material"]`} {
		rec, w := searchAs(t, platform.RelationStudent, docTypes, "student")
		if w.Code != http.StatusOK || len(rec.filters) != 3 || rec.embeds != 1 {
			t.Fatalf("%s: %d, filters %v", docTypes, w.Code, rec.filters)
		}
		for _, f := range rec.filters {
			if !slices.Equal(f.docTypes, []string{docTeachingMaterial}) || f.org != "org" || f.classroom != classID {
				t.Errorf("%s: a student searched with %+v", docTypes, f)
			}
		}
	}
}

func TestATeachersSearchFilterPassesThrough(t *testing.T) {
	for docTypes, want := range map[string][]string{`["rubric"]`: {"rubric"}, `null`: nil, `["marking_scheme","teaching_material"]`: {"marking_scheme", "teaching_material"}} {
		rec, w := searchAs(t, platform.RelationTeacher, docTypes, "teacher")
		if w.Code != http.StatusOK || len(rec.filters) != 3 {
			t.Fatalf("%s: %d %v", docTypes, w.Code, rec.filters)
		}
		for _, f := range rec.filters {
			if !slices.Equal(f.docTypes, want) {
				t.Errorf("%s: searched with %v", docTypes, f.docTypes)
			}
		}
	}
}

func TestOnlyTheClassroomsTeacherUploads(t *testing.T) {
	cases := []struct {
		name, relation string
		roles          []string
	}{
		{"student", platform.RelationStudent, []string{"student"}},
		{"teacher token, student relation", platform.RelationStudent, []string{"teacher"}},
		{"stranger", "", []string{"teacher"}},
		{"student token, teacher relation", platform.RelationTeacher, []string{"student"}},
	}
	for _, tc := range cases {
		var inserted newDocument
		files := &fileStore{}
		s := server{store: uploadStore{inserted: &inserted}, access: platformtest.Access(tc.relation), files: files}
		r := platformtest.Request(platformtest.Claims("user", tc.roles...), http.MethodPost, "/v1/documents?classroom_id="+classID+"&title=Notes", "Photosynthesis makes glucose.")
		r.Header.Set("Content-Type", mimeText)
		if w := platformtest.ServeRequest(s.routes, r); w.Code != http.StatusForbidden || len(files.puts) != 0 || inserted.ID != "" {
			t.Errorf("%s: %d, stored %v %q", tc.name, w.Code, files.puts, inserted.ID)
		}
	}
}

func TestAPDFOverTheLimitIsTooLarge(t *testing.T) {
	var inserted newDocument
	files := &fileStore{}
	pdf := pdfMagic + "1.7\n" + strings.Repeat("x", maxPDF)
	w := uploadBody(t, uploadStore{inserted: &inserted}, files, "&title=Notes", mimePDF, pdf)
	if w.Code != http.StatusRequestEntityTooLarge || !strings.Contains(w.Body.String(), "UPLOAD_TOO_LARGE") || len(files.puts) != 0 || inserted.ID != "" {
		t.Fatalf("%d %s, stored %v", w.Code, w.Body.String(), files.puts)
	}
}

const figure = "0123456789abcdef.png"

func (documentStore) findIndexed(ctx context.Context, org, id, image string) (Document, error) {
	if image != figure {
		return Document{}, platform.ErrNotFound
	}
	d, _, err := documentStore{}.findDocument(ctx, org, id)
	return d, err
}

// figurefiles serves any object and records the keys opened.
type figureFiles struct {
	documentFiles
	opened []string
}

func (f *figureFiles) open(_ context.Context, key string) (io.ReadCloser, int64, error) {
	f.opened = append(f.opened, key)
	return io.NopCloser(strings.NewReader("png")), 3, nil
}

func TestStudentsCannotSeeRubricFigures(t *testing.T) {
	cases := []struct {
		name, relation, id, image string
		roles                     []string
		status                    int
	}{
		{"teacher, rubric", platform.RelationTeacher, rubricID, figure, []string{"teacher"}, http.StatusOK},
		{"student, teaching material", platform.RelationStudent, teachingID, figure, []string{"student"}, http.StatusOK},
		{"student, rubric", platform.RelationStudent, rubricID, figure, []string{"student"}, http.StatusNotFound},
		{"teacher token, student relation, rubric", platform.RelationStudent, rubricID, figure, []string{"teacher"}, http.StatusNotFound},
		{"no access", "", teachingID, figure, []string{"student"}, http.StatusForbidden},
		{"unknown figure", platform.RelationTeacher, rubricID, "fedcba9876543210.png", []string{"teacher"}, http.StatusNotFound},
		{"malformed figure", platform.RelationTeacher, rubricID, "..%2Fkey", []string{"teacher"}, http.StatusNotFound},
	}
	for _, tc := range cases {
		files := &figureFiles{}
		s := server{store: documentStore{}, access: platformtest.Access(tc.relation), files: files}
		w := platformtest.Serve(s.routes, http.MethodGet, "/v1/documents/"+tc.id+"/figures/"+tc.image, "", "user", tc.roles...)
		served := w.Code == http.StatusOK
		if w.Code != tc.status || served != (len(files.opened) == 1) {
			t.Errorf("%s: %d, opened %v", tc.name, w.Code, files.opened)
		}
		if served && (files.opened[0] != figureKey("org", tc.id, figure) || w.Body.String() != "png") {
			t.Errorf("%s: served %v %q", tc.name, files.opened, w.Body.String())
		}
	}
}
