package main

import (
	"encoding/json"
	"fmt"
	"learning/platform"
	"learning/platform/platformtest"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// the question bank and resit routes (adr 0017), against the memory store.

// mixed is a published assessment with a short answer (teacher-marked, with a marking point and a source)
// and a numerical question, with ai marking on.
const mixed = `{"classroom_id":"` + room + `","title":"Mixed","status":"published","ai_marking":true,"questions":[
	{"prompt":"Capital of France?","type":"short_answer","accepted_answers":["Paris"],"topic_id":"` + topic + `",
	 "marking_scheme":[{"text":"Paris only"}],"sources":[{"text":"France's capital is Paris."}],"origin":"generated"},
	{"prompt":"1 + 1?","type":"numerical","options":[],"answer":"2","tolerance":"0","topic_id":"` + topic + `"}]}`

// bankServer is a teacher's server whose classroom has user and amy on its roster.
func bankServer(t *testing.T) (server, *memoryStore) {
	t.Helper()
	s, store := newServer(platform.RelationTeacher, topicValid)
	s.roster = platformtest.Roster{"user", "amy"}
	return s, store
}

func teacherAssessment(t *testing.T, w *httptest.ResponseRecorder, status int) TeacherAssessment {
	t.Helper()
	var v TeacherAssessment
	if w.Code != status || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	return v
}

func bankQuestion(t *testing.T, w *httptest.ResponseRecorder, status int) BankQuestion {
	t.Helper()
	var v BankQuestion
	if w.Code != status || json.Unmarshal(w.Body.Bytes(), &v) != nil {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	return v
}

func resitBody(students string, copies, banks []string) string {
	ids := func(list []string) string {
		b, _ := json.Marshal(list)
		return string(b)
	}
	return fmt.Sprintf(`{"student_ids":%s,"copy_question_ids":%s,"bank_question_ids":%s}`, students, ids(copies), ids(banks))
}

func TestSavingToTheBankKeepsNoPassagesAndLinksTheQuestion(t *testing.T) {
	s, _ := bankServer(t)
	v := post(t, s, mixed)
	q := v.Questions[0]
	body := `{"assessment_id":"` + v.ID + `","question_id":"` + q.ID + `"}`
	saved := bankQuestion(t, call(t, s, http.MethodPost, "/v1/bank/questions", body, "teacher"), http.StatusCreated)
	if saved.Prompt != q.Prompt || saved.Origin != originGenerated || saved.ID == q.ID {
		t.Fatalf("%+v", saved)
	}
	if w := call(t, s, http.MethodPost, "/v1/bank/questions", body, "teacher"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "ALREADY_IN_BANK") {
		t.Fatalf("saved twice: %d %s", w.Code, w.Body.String())
	}
	got := teacherAssessment(t, call(t, s, http.MethodGet, "/v1/assessments/"+v.ID, "", "teacher"), http.StatusOK)
	if got.Questions[0].BankQuestion != saved.ID || got.Questions[1].BankQuestion != "" {
		t.Fatalf("the links are %+v", got.Questions)
	}
	if strings.Contains(call(t, s, http.MethodGet, "/v1/bank/questions/"+saved.ID, "", "teacher").Body.String(), "Paris only") {
		t.Fatal("the bank kept a marking point")
	}
	for name, c := range map[string]struct {
		body   string
		status int
		code   string
	}{
		"vanished question":  {`{"assessment_id":"` + v.ID + `","question_id":"` + topic + `"}`, http.StatusConflict, "ASSESSMENT_CHANGED"},
		"unknown assessment": {`{"assessment_id":"` + topic + `","question_id":"` + q.ID + `"}`, http.StatusNotFound, ""},
		"both":               {`{"question":{},"assessment_id":"` + v.ID + `","question_id":"` + q.ID + `"}`, http.StatusUnprocessableEntity, ""},
		"passages":           {`{"question":{"prompt":"Why?","type":"short_answer","accepted_answers":["x"],"topic_id":"` + topic + `","sources":[{"text":"a"}]}}`, http.StatusUnprocessableEntity, ""},
		"invalid":            {`{"question":{"prompt":"","type":"numerical","topic_id":"` + topic + `"}}`, http.StatusUnprocessableEntity, ""},
	} {
		w := call(t, s, http.MethodPost, "/v1/bank/questions", c.body, "teacher")
		if w.Code != c.status || !strings.Contains(w.Body.String(), c.code) {
			t.Errorf("%s: %d %s", name, w.Code, w.Body.String())
		}
	}
}

func TestOnlyItsTeacherReachesABankQuestion(t *testing.T) {
	s, store := bankServer(t)
	written := `{"question":{"prompt":"Why?","type":"free_response","topic_id":"` + topic + `","rubric":{"criteria":[{"title":"Reason","levels":[{"points":0,"descriptor":"None"},{"points":3,"descriptor":"Clear"}]}]}}}`
	saved := bankQuestion(t, call(t, s, http.MethodPost, "/v1/bank/questions", written, "teacher"), http.StatusCreated)
	if saved.Points != 3 || saved.MaxLength != defaultMaxLength || saved.Rubric.Criteria[0].ID != "c1" {
		t.Fatalf("the defaults were not filled in: %+v", saved)
	}
	theirs := store.bank[saved.ID]
	theirs.owner = "someone-else"
	store.bank["0f0f0f0f-0000-4000-8000-000000000001"] = theirs
	other := "/v1/bank/questions/0f0f0f0f-0000-4000-8000-000000000001"
	for _, method := range []string{http.MethodGet, http.MethodDelete} {
		if w := call(t, s, method, other, "", "teacher"); w.Code != http.StatusNotFound {
			t.Errorf("%s another teacher's: %d", method, w.Code)
		}
	}
	if w := call(t, s, http.MethodPut, other, `{"prompt":"Mine?","type":"numerical","answer":"1","tolerance":"0","topic_id":"`+topic+`"}`, "teacher"); w.Code != http.StatusNotFound {
		t.Errorf("changed another teacher's: %d", w.Code)
	}
	for _, path := range []string{"/v1/bank/questions", "/v1/bank/questions/" + saved.ID} {
		if w := call(t, s, http.MethodGet, path, "", "student"); w.Code != http.StatusForbidden {
			t.Errorf("a student read %s: %d", path, w.Code)
		}
	}
	changed := bankQuestion(t, call(t, s, http.MethodPut, "/v1/bank/questions/"+saved.ID, `{"prompt":"2 + 2?","type":"numerical","answer":"4","tolerance":"0","topic_id":"`+topic+`"}`, "teacher"), http.StatusOK)
	if changed.ID != saved.ID || changed.Prompt != "2 + 2?" || changed.Points != 1 {
		t.Fatalf("%+v", changed)
	}
	if w := call(t, s, http.MethodPut, "/v1/bank/questions/"+saved.ID, `{"prompt":"x","type":"numerical","answer":"4","tolerance":"0","topic_id":"`+topic+`","marking_scheme":[{"text":"m"}]}`, "teacher"); w.Code != http.StatusUnprocessableEntity {
		t.Errorf("a passage was added: %d", w.Code)
	}
	if w := call(t, s, http.MethodDelete, "/v1/bank/questions/"+saved.ID, "", "teacher"); w.Code != http.StatusNoContent {
		t.Fatalf("%d", w.Code)
	}
	s.topics = fixedTopics(topicInvalid)
	if w := call(t, s, http.MethodPost, "/v1/bank/questions", written, "teacher"); w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "INVALID_TOPIC") {
		t.Errorf("an archived topic was banked: %d", w.Code)
	}
}

func TestTheBankFillsUp(t *testing.T) {
	s, store := bankServer(t)
	store.bank = map[string]memoryBankQuestion{}
	for i := range maxBank {
		store.bank[fmt.Sprint(i)] = memoryBankQuestion{owner: "user"}
	}
	w := call(t, s, http.MethodPost, "/v1/bank/questions", `{"question":{"prompt":"1?","type":"numerical","answer":"1","tolerance":"0","topic_id":"`+topic+`"}}`, "teacher")
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "BANK_FULL") {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}

func TestTheBankListSaysWhatAStudentAnswered(t *testing.T) {
	s, _ := bankServer(t)
	v := post(t, s, mixed)
	saved := bankQuestion(t, call(t, s, http.MethodPost, "/v1/bank/questions", `{"assessment_id":"`+v.ID+`","question_id":"`+v.Questions[1].ID+`"}`, "teacher"), http.StatusCreated)
	s.access = platformtest.Access(platform.RelationStudent)
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/submissions", mixedAnswers(v), "student"); w.Code != http.StatusCreated {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	s.access = platformtest.Access(platform.RelationTeacher)
	var page platform.ListPage[BankQuestion]
	w := call(t, s, http.MethodGet, "/v1/bank/questions?classroom_id="+room+"&student_id=user&type=numerical", "", "teacher")
	if w.Code != http.StatusOK || json.Unmarshal(w.Body.Bytes(), &page) != nil || page.Total != 1 || page.Items[0].ID != saved.ID || !*page.Items[0].Answered {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	for query, status := range map[string]int{
		"?classroom_id=" + room + "&student_id=user&exclude_answered=true": http.StatusOK,
		"?classroom_id=" + room + "&student_id=ghost":                      http.StatusNotFound,
		"?student_id=user":               http.StatusUnprocessableEntity,
		"?exclude_answered=true":         http.StatusUnprocessableEntity,
		"?type=essay":                    http.StatusUnprocessableEntity,
		"?origin=written":                http.StatusUnprocessableEntity,
		"?topic_id=nope":                 http.StatusUnprocessableEntity,
		"?q=" + strings.Repeat("a", 201): http.StatusUnprocessableEntity,
		"?limit=101":                     http.StatusUnprocessableEntity,
	} {
		if w := call(t, s, http.MethodGet, "/v1/bank/questions"+query, "", "teacher"); w.Code != status {
			t.Errorf("%s: %d, want %d", query, w.Code, status)
		}
	}
	s.access = platformtest.Access("")
	if w := call(t, s, http.MethodGet, "/v1/bank/questions?classroom_id="+room+"&student_id=user", "", "teacher"); w.Code != http.StatusForbidden {
		t.Errorf("a teacher of another class learned what a student answered: %d", w.Code)
	}
}

func TestMakingAResit(t *testing.T) {
	s, _ := bankServer(t)
	v := post(t, s, mixed)
	saved := bankQuestion(t, call(t, s, http.MethodPost, "/v1/bank/questions", `{"question":{"prompt":"3 + 3?","type":"numerical","answer":"6","tolerance":"0","topic_id":"`+topic+`"}}`, "teacher"), http.StatusCreated)
	path := "/v1/assessments/" + v.ID + "/resits"
	resit := teacherAssessment(t, call(t, s, http.MethodPost, path, resitBody(`["amy"]`, []string{v.Questions[1].ID, v.Questions[0].ID}, []string{saved.ID}), "teacher"), http.StatusCreated)
	if resit.Status != statusDraft || resit.ResitOf != v.ID || resit.Title != "Mixed (resit)" || len(resit.Assignees) != 1 || resit.Assignees[0] != "amy" ||
		len(resit.Questions) != 3 || resit.Questions[0].Prompt != "1 + 1?" || resit.Questions[2].BankQuestion != saved.ID || !resit.AIMarking {
		t.Fatalf("%+v", resit)
	}
	if resit.Questions[1].ID == v.Questions[0].ID || len(resit.Questions[1].MarkingScheme) != 1 || len(resit.Questions[1].Sources) != 1 {
		t.Fatalf("a copy lost its passages or kept its id: %+v", resit.Questions[1])
	}
	// without a teacher-marked question, ai marking is dropped.
	numeric := teacherAssessment(t, call(t, s, http.MethodPost, path, resitBody(`["amy"]`, []string{v.Questions[1].ID}, nil), "teacher"), http.StatusCreated)
	if numeric.AIMarking {
		t.Fatal("ai marking stayed on without a teacher-marked question")
	}
	many := make([]string, 51)
	for i := range many {
		many[i] = fmt.Sprintf("0f0f0f0f-0000-4000-8000-%012d", i)
	}
	for name, c := range map[string]struct {
		body   string
		status int
		code   string
	}{
		"no questions":         {resitBody(`["amy"]`, nil, nil), http.StatusUnprocessableEntity, ""},
		"too many questions":   {resitBody(`["amy"]`, nil, many), http.StatusUnprocessableEntity, ""},
		"a repeated question":  {resitBody(`["amy"]`, []string{v.Questions[1].ID, v.Questions[1].ID}, nil), http.StatusUnprocessableEntity, ""},
		"a vanished question":  {resitBody(`["amy"]`, []string{topic}, nil), http.StatusConflict, "ASSESSMENT_CHANGED"},
		"not my bank question": {resitBody(`["amy"]`, nil, []string{topic}), http.StatusUnprocessableEntity, "INVALID_BANK_QUESTION"},
		"nobody":               {resitBody(`[]`, []string{v.Questions[1].ID}, nil), http.StatusUnprocessableEntity, ""},
		"a repeated student":   {resitBody(`["amy","amy"]`, []string{v.Questions[1].ID}, nil), http.StatusUnprocessableEntity, ""},
		"not enrolled":         {resitBody(`["ghost"]`, []string{v.Questions[1].ID}, nil), http.StatusUnprocessableEntity, "STUDENT_NOT_ENROLLED"},
		"server fields":        {`{"student_ids":["amy"],"copy_question_ids":["` + v.Questions[1].ID + `"],"assigned":true}`, http.StatusUnprocessableEntity, ""},
	} {
		w := call(t, s, http.MethodPost, path, c.body, "teacher")
		if w.Code != c.status || !strings.Contains(w.Body.String(), c.code) {
			t.Errorf("%s: %d %s", name, w.Code, w.Body.String())
		}
	}
	draft := post(t, s, draftQuiz)
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+draft.ID+"/resits", resitBody(`["amy"]`, []string{draft.Questions[0].ID}, nil), "teacher"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "NOT_PUBLISHED") {
		t.Errorf("a draft was resat: %d", w.Code)
	}
	if w := call(t, s, http.MethodPost, path, resitBody(`["amy"]`, []string{v.Questions[1].ID}, nil), "student"); w.Code != http.StatusForbidden {
		t.Errorf("a student made a resit: %d", w.Code)
	}
	s.topics = fixedTopics(topicInvalid)
	if w := call(t, s, http.MethodPost, path, resitBody(`["amy"]`, nil, []string{saved.ID}), "teacher"); w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "INVALID_TOPIC") {
		t.Errorf("a bank question on an archived topic was added: %d", w.Code)
	}
	// a resit can itself be resat, once published.
	s.topics = fixedTopics(topicValid)
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+resit.ID+"/publish", "", "teacher"); w.Code != http.StatusOK {
		t.Fatal(w.Code)
	}
	again := teacherAssessment(t, call(t, s, http.MethodPost, "/v1/assessments/"+resit.ID+"/resits", resitBody(`["amy"]`, []string{resit.Questions[2].ID}, nil), "teacher"), http.StatusCreated)
	if again.ResitOf != resit.ID || again.Questions[0].BankQuestion != saved.ID {
		t.Fatalf("a resit of a resit lost its link: %+v", again)
	}
}

func TestAnUnassignedStudentReachesNoResitPath(t *testing.T) {
	s, store := bankServer(t)
	v := post(t, s, quiz)
	resit := teacherAssessment(t, call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/resits", resitBody(`["amy"]`, []string{v.Questions[0].ID}, nil), "teacher"), http.StatusCreated)
	if w := call(t, s, http.MethodPost, "/v1/assessments/"+resit.ID+"/publish", "", "teacher"); w.Code != http.StatusOK {
		t.Fatal(w.Code)
	}
	s.access = platformtest.Access(platform.RelationStudent)
	answers := `{"answers":{"` + resit.Questions[0].ID + `":"1"}}`
	for name, c := range map[string][3]string{
		"open":        {http.MethodGet, "/v1/assessments/" + resit.ID, ""},
		"batch":       {http.MethodPost, "/v1/assessments/batch", `{"ids":["` + resit.ID + `"]}`},
		"submit":      {http.MethodPost, "/v1/assessments/" + resit.ID + "/submissions", answers},
		"submission":  {http.MethodGet, "/v1/assessments/" + resit.ID + "/submission", ""},
		"read saved":  {http.MethodGet, "/v1/assessments/" + resit.ID + "/saved-answers", ""},
		"write saved": {http.MethodPut, "/v1/assessments/" + resit.ID + "/saved-answers", answers},
	} {
		if w := call(t, s, c[0], c[1], c[2], "student"); w.Code != http.StatusNotFound {
			t.Errorf("%s: %d %s", name, w.Code, w.Body.String())
		}
	}
	w := call(t, s, http.MethodGet, "/v1/assessments/page?classroom_id="+room, "", "student")
	if w.Code != http.StatusOK || strings.Contains(w.Body.String(), resit.ID) || !strings.Contains(w.Body.String(), `"total":1`) {
		t.Fatalf("the resit is listed or counted: %s", w.Body.String())
	}
	// once on the list, the student sees it labelled a resit.
	r := store.assessments[resit.ID]
	r.Assignees = []string{"amy", "user"}
	store.assessments[resit.ID] = r
	if w = call(t, s, http.MethodGet, "/v1/assessments/"+resit.ID, "", "student"); w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"resit":true`) {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w = call(t, s, http.MethodGet, "/v1/assessments?classroom_id="+room, "", "student"); !strings.Contains(w.Body.String(), `"resit":true`) {
		t.Fatalf("the list does not say it is a resit: %s", w.Body.String())
	}
}

func TestChangingWhoIsAssigned(t *testing.T) {
	s, store := bankServer(t)
	v := post(t, s, quiz)
	resit := teacherAssessment(t, call(t, s, http.MethodPost, "/v1/assessments/"+v.ID+"/resits", resitBody(`["amy","user"]`, []string{v.Questions[0].ID}, nil), "teacher"), http.StatusCreated)
	path := "/v1/assessments/" + resit.ID + "/assignees"
	store.submitted[resit.ID+"/user"] = Submission{Student: "user", SubmittedAt: time.Now()}
	for name, c := range map[string]struct {
		path, body string
		status     int
		code       string
	}{
		"nobody":            {path, `{"student_ids":[]}`, http.StatusUnprocessableEntity, ""},
		"not enrolled":      {path, `{"student_ids":["amy","user","ghost"]}`, http.StatusUnprocessableEntity, "STUDENT_NOT_ENROLLED"},
		"removing a submit": {path, `{"student_ids":["amy"]}`, http.StatusConflict, "HAS_SUBMISSIONS"},
		"whole class":       {"/v1/assessments/" + v.ID + "/assignees", `{"student_ids":["amy"]}`, http.StatusConflict, "NOT_ASSIGNED"},
	} {
		w := call(t, s, http.MethodPut, c.path, c.body, "teacher")
		if w.Code != c.status || !strings.Contains(w.Body.String(), c.code) {
			t.Errorf("%s: %d %s", name, w.Code, w.Body.String())
		}
	}
	// user submitted and then left the class: kept without a roster check.
	s.roster = platformtest.Roster{"amy"}
	got := teacherAssessment(t, call(t, s, http.MethodPut, path, `{"student_ids":["user"]}`, "teacher"), http.StatusOK)
	if len(got.Assignees) != 1 || got.Assignees[0] != "user" {
		t.Fatalf("%+v", got.Assignees)
	}
	var progress []Progress
	w := call(t, s, http.MethodGet, "/v1/assessments/progress?classroom_id="+room, "", "teacher")
	if json.Unmarshal(w.Body.Bytes(), &progress) != nil {
		t.Fatal(w.Body.String())
	}
	for _, p := range progress {
		if p.ID == resit.ID && (p.ResitOf == nil || *p.ResitOf != v.ID) {
			t.Fatalf("progress does not name the original: %+v", p)
		}
	}
}

// the server alone sets whether an assessment is assigned, what it resits and who is on it.
func TestCreateAndEditCannotSetTheServersFields(t *testing.T) {
	s, _ := bankServer(t)
	for _, field := range []string{`"assigned":true`, `"resit_of":"` + room + `"`, `"assignees":["amy"]`, `"id":"` + room + `"`, `"published_at":null`} {
		body := strings.Replace(quiz, `"title":"Fractions"`, `"title":"Fractions",`+field, 1)
		if w := call(t, s, http.MethodPost, "/v1/assessments", body, "teacher"); w.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d", field, w.Code)
		}
	}
}
