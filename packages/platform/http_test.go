package platform

import (
	"net/http/httptest"
	"strings"
	"testing"
)

func TestStrictJSON(t *testing.T) {
	for _, body := range []string{`{"extra":1}`, `{} {}`, `null []`} {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", "/", strings.NewReader(body))
		var value struct{}
		if Decode(w, r, &value) {
			t.Fatalf("accepted %s", body)
		}
	}
}
func TestRoles(t *testing.T) {
	var c Claims
	c.Realm.Roles = []string{"student"}
	if c.Has("teacher") || !c.Has("student") {
		t.Fatal("incorrect role authorization")
	}
}
