import { DestroyRef, inject, type Signal, type WritableSignal } from '@angular/core';
import { GradingApi } from '../../api/clients';
import type { Evaluation, Evidence } from '../../api/models';

/* how long an open grade first waits before re-reading ai suggestions still being prepared, and the longest wait. */
const SUGGESTION_POLL = 5000;
const SUGGESTION_POLL_MAX = 60000;

/* grade with the suggestions of fresh, a re-read of it; its marks, saved or not, stay. */
export function withSuggestions(grade: Evaluation, fresh: Evaluation): Evaluation {
  const suggestions = new Map(fresh.evidence.map((e) => [e.question_id, e.suggestion]));
  return {
    ...grade,
    evidence: grade.evidence.map((e) => ({ ...e, suggestion: suggestions.get(e.question_id) })),
  };
}

/* while a suggestion of the grade being marked, or its second check, is being prepared, re-reads the grade so it appears without a refresh, less often the longer it takes; made in an injection context. */
export class SuggestionPoll {
  private readonly grading = inject(GradingApi);
  private poll: ReturnType<typeof setTimeout> | undefined;

  constructor(
    private readonly selected: WritableSignal<Evaluation | null>,
    private readonly classroomId: Signal<string>,
    /* puts a re-read grade's suggestions in its queue row too, so the row's chips follow them. */
    private readonly reread: (fresh: Evaluation) => void = () => undefined,
  ) {
    inject(DestroyRef).onDestroy(() => this.stop());
  }

  stop(): void {
    clearTimeout(this.poll);
  }

  watch(delay = SUGGESTION_POLL): void {
    clearTimeout(this.poll);
    const grade = this.selected();
    const waiting = (e: Evidence) =>
      e.suggestion?.status === 'pending' ||
      (grade?.status === 'draft' && e.suggestion?.review?.status === 'pending');
    if (!grade?.evidence.some(waiting)) return;
    const classroomId = this.classroomId();
    this.poll = setTimeout(() => {
      void this.grading.evaluation(grade.id).then(
        (fresh) => {
          const current = this.selected();
          if (current?.id !== grade.id || this.classroomId() !== classroomId) return;
          // only the suggestions change here, so the teacher's unsaved marks stay.
          this.selected.set(withSuggestions(current, fresh));
          this.reread(fresh);
          this.watch(Math.min(delay * 2, SUGGESTION_POLL_MAX));
        },
        () => {
          if (this.selected()?.id === grade.id) this.watch(Math.min(delay * 2, SUGGESTION_POLL_MAX));
        },
      );
    }, delay);
  }
}
