---
project: Learning Ledger
profile: product-admin
locale: en-SG
---
# Learning Ledger design context

## North Star
A teacher's working notebook: calm, precise, and centered on the evidence behind learning. Teachers organize lessons and review actual submissions; students see their own work. This is a product workspace, not a marketing landing page. The supplied brief in docs/requirements.md is authoritative.

## Visual direction
Direction A, "Refined Ledger": a navy sidebar, teal brand, serif page titles and calm cards on a light canvas. The classroom → materials → assessment → evidence rail appears only on the signed-out welcome card (`apps/web/src/app.html:176`).

Teachers start on a Class dashboard that shows only real data for the current classroom. The subtitle reads "<N> students · <M> published assessments", with a primary "New assessment" link. Three tiles show Needs grading (drafts waiting), Students enrolled and Published assessments. "Needs grading" is highlighted (`.stat-highlight`) and captioned "Start marking" while drafts wait. An "Assessments" table (Assessment, Submitted with a bar and "8 of 9", Due) links each title to that assessment's grading queue. A class topics table shows each topic's percent and "3 of 4", banded under 50% (`--danger-soft`), 50–79% (`--warning-soft`) and 80% and up (`--good-soft`), with a legend. A topic with fewer than five questions gets a dashed edge, one that needs practice an alert icon, and one with no evidence a dash; each has hidden text. We rejected a generic KPI dashboard with trends, scores or predictions, because nothing is known about a class until grades are finalized. Every tile links to the page where the teacher acts on it. Under 700px each tile is one row, its label and caption beside the number, so the tables stay near the top. The class topics table pins its topic column while the students scroll sideways.

Students start on Home. On wide screens the to-do list sits on the left and everything else on the right, from 1100px. Due chips are danger when overdue and warning when due within three days. Only the first Start button is primary. Home lists what they still have to submit, how many submissions wait for the teacher, their latest finalized results and the topics to practise. It shows a grade only after the teacher finalizes it. While there are no finalized results and no topic evidence, one "Results and practice" panel says so in place of two empty ones. The username note shows only while the student is in no classroom.

## Shell and navigation
- The sidebar is 232px wide (`styles.css:401`). It holds the brand, the "Current classroom" select, grouped navigation with an icon per item, the shell's status line and the account block (name, role, Profile link, Sign out). Students also get a "Refresh classrooms" icon button beside the select.
- The current item has the `--navy-2` fill and a 3px `--accent` bar on its left. Grading review shows a count badge of drafts waiting, with hidden text "N grades to review".
- Navigation groups, in order (`apps/web/src/app.routes.ts`):
  - Teacher: Class dashboard; **Teach**: Assessments, Question bank, Teaching materials, Students, Curriculum; **Review**: Grading review, Learning performance; **Assistant**: Ask the materials; **Setup**: Classrooms.
  - Student: Home; **My work**: Assessments, My results, My progress; **Learn**: Teaching materials, Ask the materials.
- The classroom choice is remembered per user in this browser (`apps/web/src/core/classroom-context.ts`). If the saved classroom is gone, or storage is blocked, the first classroom in the list is chosen.
- A 56px sticky top bar (`styles.css:619`) holds the AI meter pill on the right.
- Under 900px the sidebar becomes a drawer, 280px wide and at most 85% of the viewport. The top bar's "Open menu" button opens it, with a backdrop and "Close menu". The workspace is inert while it is open. The top bar also shows the current classroom's name.
- Teachers start on `/dashboard`; students on `/home`.

## Tokens and ownership
Runtime source of truth: apps/web/src/styles.css `:root`. This document records intent; CSS owns exact token values.

- Surfaces and text: `--canvas` #f3f6f8, `--surface` #ffffff, `--ink` #182f45, `--muted` #566a7b, `--line` #d6e0e6 (dividers and card borders), `--control` #7a909e (input borders).
- Sidebar: `--navy` #142a3d, `--navy-2` #1f3a52 (current item), `--navy-hover` #1a3349, `--navy-text` #c9d7e1, `--navy-muted` #a5b8c6 (group labels), `--on-dark` #ffffff, `--danger-on-dark` #ffb4bc.
- Brand: `--brand` #176b70, `--brand-hover` #10565a, `--brand-soft` #e3f0f0, `--accent` #91d4d2.
- Status: `--danger` #a52b36, `--danger-hover` #7f1f29, `--danger-soft` #f8e7e9; `--warning` #7a4f00, `--warning-soft` #fbf1dc; `--good` #2c6e3f, `--good-soft` #e5f2e8; `--focus` #b3780b.
- AI drafts: `--ai-tint` #eaf0f4, with `--ai-edge` #678b9b as a 4px left edge on AI-written content.
- Shape: `--radius-sm` 6px (controls), `--radius` 10px (cards), `--radius-lg` 12px (dialogs). `--shadow-pop` is for dialogs, popovers and the drawer only. `--backdrop` sits behind the drawer and dialogs.
- Spacing uses 4/8/12/16/24/32px. No external fonts or decorative imagery.

## Typography
Sizes come from the `--text-*` tokens (label 11px, xs 13px, sm 14px, base 16px, lg 17px, xl 22px, 2xl 28px, display 32px); no other size is set. Nothing but uppercase labels goes under 13px. Body is `'Segoe UI', system-ui, sans-serif` at 16px / 1.55. Page titles are Georgia `h2` at 28px, 22px under 700px. Card headings are 17px, weight 600. Tables and dense lists are 14px. Uppercase labels are 11px with 0.08em letter spacing (group labels, stacked-table cell labels). Identifiers are set in Consolas. Numbers in tables use tabular figures.

## Components
- **Buttons** (`styles.css:177-309`): a plain `button` is primary (filled teal), one per page or form. `.secondary` is outlined. `.quiet` is text-only, for low-weight actions such as discard, close or clear. `.danger` is filled red, for destructive confirms only. `.icon-button` is a 44×44 square holding one icon. A link that leads to an action is `a.button-link` (it looks secondary) or `a.button-link.primary`. A disabled button has the `--canvas` fill, `--line` border and `--muted` text (no fill when quiet), never a faded copy of its enabled look.
- **Icons:** `<app-icon>` (`shared/icon.ts`), inline SVG paths from Lucide (ISC licence). No icon font or CDN.
- **Page header:** `<app-page-header>` (`shared/page-header.ts`) with a title, a one-line subtitle and actions on the right. Icon-only actions stay on the title row; text buttons wrap under it.
- **Refresh:** `<app-refresh-button>` (`shared/refresh-button.ts`), an outlined icon button named by its label (for example "Refresh assessments"). Its icon spins while busy.
- **Chips** (`.badge`): plain teal, `.badge-good`, `.badge-warning`, `.badge-danger` and `.badge-plain`. `.ai-chip` carries the sparkles icon and reads "AI-written", "AI-written draft" or "AI draft for your review". A chip always has text.
- **Filter toggles:** Grading review's status filters (All, To mark, Drafts, Finalized) are `.filter-chips` buttons with `aria-pressed`, 44px high. They are buttons, not chips.
- **Cards:** `.panel`, with a `--line` border and `--radius`, and no shadow.
- **Tables:** `.stack-table` turns each row into a card under 700px, with each cell labelled by its column name (`data-label`). `.table-scroll` scrolls sideways instead; the class topics table uses it.
- **Sticky action bars:** `.answer-bar` (the assessment being taken), `.editor-bar` (the editor) and `.marking-bar` (marking) share one rule (`styles.css:2070-2116`). They stack under 700px, and the page keeps a focused control clear of them. The editor's bar also shows the due date ("No due date" when none is set), which moves to Schedule and grading.
- **Dialogs:** `<app-confirm-dialog>` (`shared/confirm-dialog.ts`). Cancel is first and focused; the destructive action is `.danger`.
- **Feedback:** `<app-status>`. An empty feedback region takes no space.
- **Method notes:** a page whose numbers need explaining keeps one line in view and the rest under a `details.method-note` "How this is measured".

## Layout and views
Content is at most 1600px wide. The assessment being taken and the teacher's preview use a centred 720px column. Each screen has one job. Form controls have visible labels, and selects are native. Assessment scheduling uses native datetime-local inputs; operating-system popup geometry is acceptable. The global scrollbar is visible, tokenized and forced-color aware.

Views are chosen by the address:
- `/assessments` is the list.
- `?edit=<id>` (or `?edit=new`) is the teacher's editor.
- `?preview=<id>` is the teacher's preview.
- `?open=<id>` is a student's assessment. A teacher's `?open=` opens the preview.
- `/question-bank` is the teacher's bank; `?edit=<id>` (or `?edit=new`) is one bank question's form.
- `/review?assessment=<id>&grade=<id>` is the grading queue with the open grade.

Back and Forward move between views.

Breakpoints:
- Under 700px: tables stack, page padding is 16px, and sticky bars stack.
- Under 900px: the sidebar becomes a drawer.
- From 1000px: the chat page fits the window. Below that it scrolls, and the conversations sit above the conversation.
- From 1100px: two columns on Grading review (a 340px queue beside the grade), on the editor (a 200px outline of questions beside the form), on Students (the roster beside All students) and on Home.
- From 1400px: the editor's generation panel becomes a 320px third column.
- Under 480px: the AI pill hides its amount visually.

Other pages:
- Classrooms is a create form above a grid of cards.
- Teaching materials has an upload card with a drop zone, above the library table.
- Curriculum is one column.
- Question bank is one column: a filter card (prompt search, topic, type), then the questions table (question, type, marks, topic with "(archived)", Edit and a Delete icon button). Its form is the editor's question fields for one question, without marking points or source passages.
- In the editor one question is open at a time: opening or adding a question folds the others, except any with an error, and an assessment opened for editing shows its first question. Each question can still be expanded or collapsed by hand. The outline wraps prompts to two lines, with the full prompt as a tooltip, and the topic select takes the widest column of the question's type, marks and topic row.
- There is no two-column `.split` layout.

## Motion and content
Only short hover and focus transitions, at most 150ms, and the drawer's slide. `prefers-reduced-motion` removes them and stops the refresh icon's spin. No animated counters or fabricated progress. Say "Finalize grade", "Create classroom" and "Upload document". AI features that are not implemented are displayed as unavailable. Document chat ("Ask the materials") answers only from indexed teaching materials and shows numbered sources. Never imply text extraction is indexing, and label model-written passages as such. Evidence uses textual counts plus observed accuracy, never mastery claims.

## Accessibility
WCAG 2.2 AA target, semantic controls, visible keyboard focus, error and live regions, and minimum 44px controls. Icon-only buttons carry the action's name as `aria-label`; those on pages also show it as a `title` tooltip (the drawer's Open menu and Close menu have the label only). Colour bands and chips always have text, so no status is shown by colour alone. Forced-colours mode outlines the current navigation item. Light theme only; English Singapore formatting; no Japanese market assumptions.
