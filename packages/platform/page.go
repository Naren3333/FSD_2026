package platform

import (
	"net/http"
	"strings"
)

// ListWindow bounds a list query.
type ListWindow struct{ Offset, Limit int }

// ListPage is the common transport envelope for a bounded list and its filtered total.
type ListPage[T any] struct {
	Items []T `json:"items"`
	Total int `json:"total"`
}

// servelist answers a list route registered twice: its /page variant reads one window (offset, limit 1 to
// most, default 20) and answers a ListPage with count's total; the legacy route answers list's bare array,
// which list bounds itself when given no window.
func ServeList[T any](w http.ResponseWriter, r *http.Request, most int, list func(...ListWindow) ([]T, error), count func() (int, error)) {
	var paging []ListWindow
	if strings.HasSuffix(r.URL.Path, "/page") {
		offset, limit, ok := Page(w, r, 20, most)
		if !ok {
			return
		}
		paging = []ListWindow{{Offset: offset, Limit: limit}}
	}
	items, err := list(paging...)
	if err != nil {
		Fail(w, err)
		return
	}
	if len(paging) == 0 {
		JSON(w, http.StatusOK, items)
		return
	}
	total, err := count()
	if err != nil {
		Fail(w, err)
		return
	}
	JSON(w, http.StatusOK, ListPage[T]{Items: items, Total: total})
}

// window uses an explicit sql window or a legacy endpoint's default bound.
func Window(windows []ListWindow, limit int) ListWindow {
	if len(windows) == 0 {
		return ListWindow{Limit: limit}
	}
	return windows[0]
}
