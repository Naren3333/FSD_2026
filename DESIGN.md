---
project: Learning Ledger
profile: product-admin
locale: en-SG
---
# Learning Ledger design context

## North Star
A teacher's working notebook: calm, precise, and centered on the evidence behind learning. Teachers organize lessons and review actual submissions; students see their own work. This is a product workspace, not a marketing landing page. The supplied brief in docs/requirements.md is authoritative.

## Visual direction
The signature is a compact learning sequence rail: classroom → materials → assessment → evidence. It describes the real workflow without invented statistics. A spacious working canvas and a persistent navy navigation column keep tools familiar. We rejected a generic KPI dashboard because no data exists until grades are finalized.

## Tokens and ownership
Runtime source of truth: apps/web/src/styles.css `:root`. This document records intent; CSS owns exact token values. Canvas #f2f5f8, surface #ffffff, ink #182f45, muted #526779, brand #176b70, border #cbd7df, danger #a52b36. Segoe UI body, Georgia display headings, Consolas identifiers. Body 16px / 1.55; restrained 32px display headings. Spacing uses 4/8/12/16/24/32px; panels 12px radius; controls 6px. No external fonts or decorative imagery.

## Layout and controls
Desktop uses a 220px navigation column with a natural document-scrolling main canvas; narrow screens stack header/navigation above the workspace. Forms have visible labels, text error messages and stable feedback regions. Native selects are intentional: operating-system popup geometry is acceptable. The global scrollbar is visible, tokenized and forced-color aware.

## Motion and content
Only short hover/focus transitions; reduced-motion removes transitions. No animated counters or fabricated progress. Say “Finalize grade”, “Create classroom”, and “Upload document”. Display AI features as unavailable; do not imply extraction is indexing. Evidence uses textual counts plus observed accuracy, never mastery claims.

## Accessibility
WCAG 2.2 AA target, semantic controls, visible keyboard focus, error/live regions, minimum 44px controls, no color-only statuses. Light theme only in the initial foundation. English Singapore formatting; no Japanese market assumptions.
