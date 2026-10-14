# Learning Ledger interaction contract

Business authority: docs/requirements.md sections 6, 8, 15–18 and docs/decisions/0001-foundation.md. Teachers own their classrooms. Students submit once; grades are immutable after teacher finalization. All authorization remains server-enforced.

| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Form | shared field component + Reactive Forms | this contract | create, submit, review | Angular tests + browser E2E |
| Select/Listbox | native select | DESIGN.md | classroom, skill, type | keyboard/browser |
| Scrollbar | global styles.css | DESIGN.md | document and horizontal tables | static + browser |
| Toast | shared status component | this contract | status, error | unit + browser |
| CRUD | feature components + Api service | API/domain contracts | create, refresh list, remove enrollment | live E2E |

Creation retains the owning view, refreshes the real list, clears only successfully saved input, and announces completion. Validation keeps entered values and focuses the first invalid control. Mutations disable their submit button. Failed operations retain input, explain the failure, and allow explicit retry; no automatic mutation retries. Authentication failures offer sign-in without deleting drafts. Tokens remain in memory through Keycloak JS.

Tab panels stay mounted to retain work; page unload warns when a form is dirty. Classroom context changes are disabled while work is unsaved. Finalization uses an explicit review reason and an app-owned confirmation dialog; cancel is the initially focused action. Removing a student from a classroom uses the same app-owned confirmation dialog with Cancel focused; it keeps their submissions and grades and is undone by enrolling them again. No irreversible delete UI in this milestone.

Lists are capped by APIs at 100 (curriculum 200); the UI states this limit. Full server pagination is a documented remaining limitation. No search or selection feature is claimed. Empty states explain the next action. All shared feedback reserves space. Errors appear inline; status has a polite live region.

Unsupported AI actions are explanatory text with disabled controls. PDFs show their indexing status (indexing, indexed with page and passage counts, or failed with a reason and a teacher-only retry); text files say they are not indexed. Chat questions stay in the field until the chatbot accepts them, answers stream with a Stop control, every answer ends answered, with no matching material, failed or stopped, and failures appear inline with the question kept for retry. Answers render the model's Markdown subset (paragraphs, lists, headings, bold, italic, code) as text, never as HTML; each source shows the whole passage the model was given, its page range, and a control that opens the original PDF at its first page. Displayed metrics come only from finalized evidence. Dates/numbers use en-SG; people, assessments, questions and skills are shown by the names their owning services return; an id is shown, as a plain identifier, only when no name exists.

