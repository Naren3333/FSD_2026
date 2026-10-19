package platform

// ListWindow bounds a list query. Optional windows preserve legacy bounded array endpoints while /page
// endpoints return a total and items. Domain filters are still explicit in each owning service's SQL.
type ListWindow struct{ Offset, Limit int }

// ListPage is the common transport envelope for a bounded list and its filtered total.
type ListPage[T any] struct {
	Items []T `json:"items"`
	Total int `json:"total"`
}

// Window uses an explicit SQL window or a legacy endpoint's default bound.
func Window(windows []ListWindow, limit int) ListWindow {
	if len(windows) == 0 {
		return ListWindow{Limit: limit}
	}
	return windows[0]
}
