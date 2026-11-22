import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { ContentApi, QuestionGenerationApi } from '../../api/clients';
import type {
  AgentExcerpt,
  Difficulty,
  DocumentRecord,
  PracticeQuestion,
  PracticeType,
  Topic,
} from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { openOriginal } from '../../shared/files';
import { focusLater } from '../../shared/focus';
import { plural } from '../../shared/format';
import {
  MAX_COUNT,
  MAX_EXISTING,
  MAX_FOCUS,
  MAX_SOURCE_DOCUMENTS,
  sourceDocuments,
} from '../../shared/generation';
import { IconComponent } from '../../shared/icon';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';

export const PRACTICE_TYPES: { value: PracticeType; label: string }[] = [
  { value: 'multiple_choice', label: 'Multiple choice' },
  { value: 'short_answer', label: 'Short answer' },
];

/* an answer as it is compared: case folded, punctuation and extra spaces gone, so "Unit-test." matches "unit test". */
export function normalizeAnswer(text: string): string {
  return text
    .normalize('NFKC')
    .toLocaleLowerCase('en-SG')
    .replace(/[\p{P}\p{S}]+/gu, ' ')
    .split(/\s+/)
    .filter(Boolean)
    .join(' ');
}

/* whether the student's answer is right: the chosen option is the key, or a short answer matches an accepted one. */
export function isCorrect(question: PracticeQuestion, answer: string): boolean {
  if (question.type === 'multiple_choice') return answer === question.answer;
  const given = normalizeAnswer(answer);
  return !!given && question.accepted_answers.some((a) => normalizeAnswer(a) === given);
}

/* one question of the set, as the student works through it. */
export interface Attempt {
  question: PracticeQuestion;
  answer: string;
  /* set once the student checks it; the key is shown only then. */
  correct: boolean | null;
}

/* "3 of 5 correct", with how many are left to answer. */
export function scoreText(attempts: readonly Attempt[]): string {
  const checked = attempts.filter((a) => a.correct !== null);
  const right = checked.filter((a) => a.correct).length;
  const left = attempts.length - checked.length;
  if (!checked.length) return `${plural(attempts.length, 'question')} to answer.`;
  return `${right} of ${checked.length} correct${left ? `, ${left} still to answer` : ''}.`;
}

/* a student's own practice from the classroom's teaching materials, written and reviewed by ai and checked in the browser; nothing is saved or graded (adr 0022). */
@Component({
  selector: 'app-practice',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    PageHeaderComponent,
    RefreshButtonComponent,
    NoClassroomComponent,
    FieldComponent,
    FieldControlDirective,
    SectionStateComponent,
    StatusComponent,
    IconComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './practice.html',
  styleUrl: './practice.css',
})
export class PracticeComponent {
  private readonly api = inject(QuestionGenerationApi);
  private readonly content = inject(ContentApi);
  private readonly usage = inject(AiUsage);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly types = PRACTICE_TYPES;
  readonly maxCount = MAX_COUNT;
  readonly maxFocus = MAX_FOCUS;
  readonly maxDocuments = MAX_SOURCE_DOCUMENTS;
  readonly maxExisting = MAX_EXISTING;
  readonly documents = new Section<DocumentRecord[]>();
  readonly topicList = new Section<Topic[]>();
  readonly choices = computed(() => sourceDocuments(this.documents.value() ?? []));
  private readonly titles = computed(
    () => new Map((this.documents.value() ?? []).map((d) => [d.id, d.title])),
  );
  readonly chosen = signal<string[]>([]);
  readonly counts = signal<Record<PracticeType, number>>({ multiple_choice: 3, short_answer: 2 });
  readonly total = computed(() =>
    PRACTICE_TYPES.reduce(
      (sum, t) => sum + (Number.isInteger(this.counts()[t.value]) ? this.counts()[t.value] : 0),
      0,
    ),
  );
  readonly totalText = computed(() => `${plural(this.total(), 'question')} in all (up to ${MAX_COUNT}).`);
  readonly topic = new FormControl('', { nonNullable: true });
  readonly difficulty = new FormControl<Difficulty>('introductory', { nonNullable: true });
  readonly focus = new FormControl('', { nonNullable: true });
  /* writing a set: its busy flag, its refusal, and "ready" once it is shown. */
  readonly state = new ActionState();
  /* why a set could not be asked for, such as no material chosen. */
  readonly note = signal('');
  readonly attempts = signal<Attempt[]>([]);
  /* how many of the questions asked for the latest set is short, and how many were asked for. */
  readonly short = signal({ missing: 0, requested: 0 });
  readonly score = computed(() => scoreText(this.attempts()));
  readonly shortText = computed(() => {
    const { missing, requested } = this.short();
    const one = missing === 1;
    return `${missing} of the ${plural(requested, 'question')} you asked for ${one ? 'was' : 'were'} left out because the checks could not confirm ${one ? 'it' : 'them'}.`;
  });
  /* a set with answers still to check, which a new set would replace. */
  readonly unfinished = computed(() => this.attempts().some((a) => a.correct === null));
  /* the replace dialog's count, worded unlike the score so the two never read the same. */
  readonly uncheckedText = computed(() => {
    const n = this.attempts().filter((a) => a.correct === null).length;
    return `${plural(n, 'question')} in this set ${n === 1 ? 'is' : 'are'} not checked yet.`;
  });
  /* confirming that a new set replaces the unfinished one; nothing runs inside the dialog itself. */
  readonly replaceState = new ActionState();
  @ViewChild('replaceDialog') replaceDialog?: ConfirmDialogComponent;
  /* the question checked without an answer, whose field says so; -1 for none. */
  readonly missing = signal(-1);
  /* failures opening a source document. */
  readonly openError = signal('');

  constructor() {
    // while a set is written the classroom stays put; a set in progress warns before the page unloads.
    const unsaved = inject(UnsavedChanges);
    unsaved.track(() => this.state.busy());
    unsaved.track(() => this.unfinished(), { locksClassroom: false });
    onClassroomChange(() => this.load());
    // topics are the school's, not the classroom's, so one read serves every classroom.
    void this.topicList.load(() => this.curriculum.refresh());
    onSectionShown('practice', () => {
      const id = this.classroomId();
      if (id) rereadQuietly(this.documents, () => this.content.documents(id));
    });
  }

  /* another classroom's materials and set never carry over. */
  load(): void {
    const id = this.classroomId();
    this.newSet();
    this.chosen.set([]);
    this.documents.clear();
    if (id) void this.documents.load(() => this.content.documents(id));
  }

  /* re-reads the materials and topics, keeping the set and the choices. */
  refresh(): void {
    const id = this.classroomId();
    if (!id) return;
    void this.documents.load(() => this.content.documents(id));
    void this.topicList.load(() => this.curriculum.refresh());
  }

  toggleDocument(id: string, on: boolean): void {
    this.chosen.update((ids) => (on ? [...ids, id] : ids.filter((d) => d !== id)));
  }

  setCount(type: PracticeType, value: number): void {
    this.counts.update((counts) => ({ ...counts, [type]: value }));
  }

  /* why the request cannot be sent, or '' when it can; a count of 0 leaves that type out. */
  problem(documentIds: string[]): string {
    if (!documentIds.length) return 'Choose at least one teaching material.';
    if (documentIds.length > MAX_SOURCE_DOCUMENTS)
      return `Choose at most ${MAX_SOURCE_DOCUMENTS} teaching materials.`;
    const counts = Object.values(this.counts());
    if (counts.some((n) => !Number.isInteger(n) || n < 0 || n > MAX_COUNT))
      return `Ask for 0–${MAX_COUNT} questions of each type.`;
    if (!this.total()) return 'Ask for at least one question.';
    if (this.total() > MAX_COUNT)
      return `Ask for at most ${MAX_COUNT} questions in all; you have ${this.total()}.`;
    if (this.focus.value.length > MAX_FOCUS) return `Keep the focus to ${MAX_FOCUS} characters.`;
    return '';
  }

  /* writes a set; with `more`, another set added after this one that leaves out the questions already shown. a new set over an unfinished one asks first, unless `replace` says it was confirmed. */
  async generate(more = false, replace = false): Promise<void> {
    if (this.state.busy()) return;
    const classroomId = this.classroomId();
    const documentIds = this.chosen().filter((id) => this.choices().some((d) => d.id === id));
    this.note.set(this.problem(documentIds));
    if (this.note() || !classroomId) {
      focusLater('practice-generate');
      return;
    }
    if (!more && !replace && this.unfinished()) {
      this.replaceDialog?.show();
      return;
    }
    const shown = this.attempts();
    let first = 0;
    let added = 0;
    await this.state.run(async () => {
      const found = await this.api
        .practice({
          classroom_id: classroomId,
          document_ids: documentIds,
          topic_id: this.topic.value || null,
          counts: Object.fromEntries(
            PRACTICE_TYPES.map((t) => [t.value, this.counts()[t.value]] as const).filter(([, n]) => n > 0),
          ),
          difficulty: this.difficulty.value,
          focus: this.focus.value.trim(),
          existing_prompts: more ? shown.map((a) => a.question.prompt).slice(-MAX_EXISTING) : [],
        })
        // a classroom change while it ran (it is locked, but never trust that) drops the reply, failed or not.
        .catch((error: unknown) => {
          if (classroomId === this.classroomId()) throw error;
          return null;
        });
      if (!found || classroomId !== this.classroomId()) return;
      const fresh = found.questions.map((question) => ({ question, answer: '', correct: null }));
      // appended to the set as it is now, so answers checked while this ran are kept.
      first = more ? this.attempts().length : 0;
      this.attempts.update((current) => (more ? [...current, ...fresh] : fresh));
      // `dropped` counts every refused draft, rewritten ones too; only what the set is short says what was left out.
      this.short.set({ missing: Math.max(found.requested - found.returned, 0), requested: found.requested });
      added = fresh.length;
    });
    // however the request ended, it may have spent ai allowance.
    this.usage.refresh();
    if (this.state.error()) {
      focusLater('practice-generate');
    } else if (added) {
      this.state.message.set(`${plural(added, 'practice question')} ready.`);
      focusLater(`practice-${first}-prompt`);
    }
  }

  /* clears the set, keeping the choices for the next one. */
  newSet(): void {
    this.attempts.set([]);
    this.short.set({ missing: 0, requested: 0 });
    this.missing.set(-1);
    this.note.set('');
    this.state.message.set('');
    this.state.error.set('');
  }

  /* the student confirmed that a new set replaces the unfinished one. */
  confirmReplace(): void {
    this.replaceDialog?.close();
    void this.generate(false, true);
  }

  /* "new set": clears it and returns to the choices. */
  clear(): void {
    this.newSet();
    focusLater('practice-generate');
  }

  setAnswer(index: number, answer: string): void {
    if (this.missing() === index) this.missing.set(-1);
    this.attempts.update((all) =>
      all.map((a, i) => (i === index && a.correct === null ? { ...a, answer } : a)),
    );
  }

  /* checks one answer, locks the question and shows its key and explanation; focus moves to the feedback. */
  check(index: number): void {
    const attempt = this.attempts()[index];
    if (!attempt || attempt.correct !== null) return;
    if (!attempt.answer.trim()) {
      this.missing.set(index);
      focusLater(`practice-${index}-answer`);
      return;
    }
    this.missing.set(-1);
    const correct = isCorrect(attempt.question, attempt.answer);
    this.attempts.update((all) => all.map((a, i) => (i === index ? { ...a, correct } : a)));
    focusLater(`practice-${index}-feedback`);
  }

  typeName(type: string): string {
    return PRACTICE_TYPES.find((t) => t.value === type)?.label ?? type;
  }

  /* where a passage is from: its document's title, and its page in a pdf. */
  source(excerpt: AgentExcerpt): string {
    const title = (excerpt.document_id && this.titles().get(excerpt.document_id)) || 'Class document';
    return excerpt.page ? `${title}, page ${excerpt.page}` : title;
  }

  /* the passage's original, at its page for a pdf. */
  open(excerpt: AgentExcerpt): void {
    const documentId = excerpt.document_id;
    if (!documentId) return;
    this.openError.set('');
    openOriginal(
      () => this.content.original(documentId),
      this.openError,
      excerpt.page ? `#page=${excerpt.page}` : '',
    );
  }

  topicName(topicId: string | null | undefined): string {
    return topicId ? this.curriculum.topicName(topicId) : '';
  }
}
