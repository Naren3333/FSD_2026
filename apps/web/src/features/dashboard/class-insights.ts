import { Component, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { PerformanceAgentApi } from '../../api/clients';
import type { ClassEvidence, ClassInsights } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { focusLater } from '../../shared/focus';
import { plural } from '../../shared/format';
import { IconComponent } from '../../shared/icon';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { requiredText } from '../../shared/validators';
import { droppedText, evidenceText, stepText } from '../performance/interpretation';

export const MAX_INSIGHTS_QUESTION = 500;
/* the agent takes this many earlier questions as context for a follow-up. */
const FOLLOW_UPS = 3;

export const PRESETS = ['Who is struggling, and with which topics?', 'What should I reteach first?'];

/* refusals that mean there was nothing to answer from, shown as an empty state rather than an error. */
const EMPTY_CODES = new Set(['NO_EVIDENCE', 'NOTHING_TO_ANSWER', 'INSIGHTS_REJECTED']);

/* "Ana Lim · Loops quiz · What does range(3) produce? · 0 of 2 marks" for a cited answer. */
export function classEvidenceText(e: ClassEvidence, student: string): string {
  return `${student} · ${evidenceText(e)}`;
}

/* asks the performance agent a question about the whole class, for its teacher to review (adr 0021). */
@Component({
  selector: 'app-class-insights',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    RouterLink,
    FieldComponent,
    FieldControlDirective,
    SectionStateComponent,
    IconComponent,
  ],
  templateUrl: './class-insights.html',
})
export class ClassInsightsComponent {
  private readonly api = inject(PerformanceAgentApi);
  private readonly usage = inject(AiUsage);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly max = MAX_INSIGHTS_QUESTION;
  readonly presets = PRESETS;
  readonly stepText = stepText;
  readonly plural = plural;
  readonly form = new FormGroup({
    question: new FormControl('', {
      nonNullable: true,
      validators: requiredText(MAX_INSIGHTS_QUESTION),
    }),
  });
  readonly tried = signal(false);
  /* the request's state only: the answer itself is in `answer`. */
  readonly result = new Section<void>();
  /* the latest answer, kept on screen when a follow-up fails or finds nothing. */
  readonly answer = signal<ClassInsights | null>(null);
  /* why there was nothing to show, such as a class with no released results. */
  readonly empty = signal('');
  /* the questions answered so far in this conversation, newest last, sent with a follow-up. */
  readonly history = signal<string[]>([]);
  readonly announcement = signal('');
  readonly evidence = computed(
    () => new Map((this.answer()?.evidence ?? []).map((e) => [e.evidence_id, e] as const)),
  );
  private readonly names = computed(
    () => new Map((this.answer()?.students ?? []).map((s) => [s.student_id, s.display_name] as const)),
  );
  readonly dropped = computed(() => droppedText(this.answer()?.dropped ?? {}, "this class's"));

  constructor() {
    // while the agent works, the classroom stays put and leaving the page warns.
    inject(UnsavedChanges).track(() => this.result.loading());
    onClassroomChange(() => this.newQuestion(false));
  }

  topic(topicId: string): string {
    return this.curriculum.topicName(topicId) || 'A topic no longer in the curriculum';
  }

  topics(topicIds: readonly string[]): string {
    return topicIds.map((id) => this.topic(id)).join(', ');
  }

  student(studentId: string): string {
    return this.names().get(studentId) ?? 'A student no longer on the roster';
  }

  cited(ids: readonly string[]): ClassEvidence[] {
    const found = this.evidence();
    return ids.flatMap((id) => found.get(id) ?? []);
  }

  evidenceText(e: ClassEvidence): string {
    return classEvidenceText(e, this.student(e.student_id));
  }

  /* fills the question with a suggested one, ready to ask or edit. */
  usePreset(question: string): void {
    this.form.controls.question.setValue(question);
    this.form.markAsDirty();
    focusLater('class-insights-question');
  }

  shortcut(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      this.ask();
    }
  }

  /* clears the answer and the earlier questions; `focus` moves to the question for the next one. */
  newQuestion(focus = true): void {
    this.result.clear();
    this.answer.set(null);
    this.empty.set('');
    this.history.set([]);
    this.announcement.set('');
    this.form.reset();
    this.tried.set(false);
    if (focus) focusLater('class-insights-question');
  }

  ask(): void {
    if (this.result.loading()) return;
    this.tried.set(true);
    this.form.markAllAsTouched();
    const classroomId = this.classroomId();
    if (this.form.invalid) {
      focusLater('class-insights-question');
      return;
    }
    if (!classroomId) return;
    const question = this.form.controls.question.value.trim();
    const previous = this.history().slice(-FOLLOW_UPS);
    this.empty.set('');
    this.announcement.set('');
    void this.result
      .load(async (current) => {
        try {
          const answer = await this.api.classInsights({
            classroom_id: classroomId,
            question,
            previous_questions: previous,
          });
          // without the curriculum, topics show a generic name.
          await this.curriculum
            .need([
              ...[...answer.struggling, ...answer.reteach].map((item) => item.topic_id),
              ...answer.observations.flatMap((item) => item.topic_ids),
            ])
            .catch(() => undefined);
          if (current()) {
            this.answer.set(answer);
            this.history.set([...previous, question].slice(-FOLLOW_UPS));
            this.form.reset();
            this.tried.set(false);
          }
        } catch (error) {
          // the question stays in the field, to edit or ask again.
          if (!(error instanceof ApiError && EMPTY_CODES.has(error.code))) throw error;
          if (current()) this.empty.set(error.message);
        }
      })
      .then(() => {
        // however the request ended, it may have spent ai allowance.
        this.usage.refresh();
        if (this.result.loading()) return;
        if (this.result.error() || this.empty()) {
          focusLater('class-insights-question');
        } else if (this.answer()) {
          this.announcement.set('Answer ready.');
          focusLater('class-insights-answer');
          // focus leaving the emptied field marks it touched: it is a fresh question, not a wrong one.
          setTimeout(() => this.form.markAsUntouched());
        }
      });
  }
}
