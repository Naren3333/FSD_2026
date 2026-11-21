import { computed, signal } from '@angular/core';
import type { Evaluation } from '../../api/models';
import { Section } from '../../shared/section';
import {
  GRADE_FILTERS,
  PAGE_SIZE,
  doubtfulFirst,
  draftAfter,
  inOrder,
  matches,
  type GradeFilter,
  type Grades,
} from './grades';

/* selection and navigation over the current page, independent of marking actions. */
export class GradeQueue {
  constructor(private readonly grades: Section<Grades>) {}
  /* the draft being marked. */
  readonly selected = signal<Evaluation | null>(null);
  /* a finalized grade shown read-only. */
  readonly viewing = signal<Evaluation | null>(null);
  /* the grade in the grade panel, being marked or read; a finalized one as the page last read it, so a release shows. */
  readonly open = computed(() => {
    const viewing = this.viewing();
    if (this.selected() || !viewing) return this.selected();
    return this.grades.value()?.grades.find((g) => g.id === viewing.id) ?? viewing;
  });
  /* the queue's status filter, applied to the loaded page only. */
  readonly gradeFilter = signal<GradeFilter>('all');
  readonly filters = GRADE_FILTERS;
  /* how many of the page's grades each filter keeps. */
  readonly counts = computed(() => {
    const grades = this.grades.value()?.grades ?? [];
    const count = (kept: GradeFilter) => grades.filter((g) => matches(g, kept)).length;
    return { all: grades.length, mark: count('mark'), draft: count('draft'), finalized: count('finalized') };
  });
  /* the queue's order as the page loaded, so rows stay put while second checks finish and grades are finalized. */
  private readonly order = signal<readonly string[]>([]);
  /* the page's grades in queue order: drafts the second check doubted first, as of the page's load. */
  readonly ordered = computed(() => {
    const grades = this.grades.value()?.grades ?? [];
    const order = this.order();
    return order.length ? inOrder(grades, order) : doubtfulFirst(grades);
  });
  /* sets the queue's order from the page just loaded. */
  fixOrder(): void {
    this.order.set(doubtfulFirst(this.grades.value()?.grades ?? []).map((g) => g.id));
  }
  /* the page's grades the filter keeps, in queue order. */
  readonly shown = computed(() => {
    const kept = this.gradeFilter();
    return this.ordered().filter((g) => matches(g, kept));
  });
  /* the next draft on the page after the open grade, whatever the filter shows. */
  readonly nextDraft = computed(() => {
    const open = this.open();
    return open ? draftAfter(this.ordered(), open.id) : null;
  });
  /* no other draft on this page, but a next page to look on. */
  readonly nextOnNextPage = computed(() => {
    const value = this.grades.value();
    return !this.nextDraft() && !!value && (value.page + 1) * PAGE_SIZE < value.total;
  });
  /* the row before the open grade in the queue as shown. */
  readonly previousGrade = computed(() => {
    const list = this.shown();
    const at = list.findIndex((g) => g.id === this.open()?.id);
    return at > 0 ? list[at - 1] : null;
  });
}
