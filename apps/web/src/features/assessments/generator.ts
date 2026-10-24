import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  computed,
  inject,
  signal,
} from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { QuestionGenerationApi } from '../../api/clients';
import type {
  Difficulty,
  DocumentRecord,
  AgentExcerpt,
  GenerateResult,
  GeneratableType,
  Question,
  GenerateReview,
  GenerateRound,
} from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { CurriculumNames } from '../../core/curriculum-names';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { MAX_PROMPT, clipText, draftQuestion, excerptLabel, fromAgent, marksText } from './editor';

/* the agent's limits (adr 0006). */
export const MAX_SOURCE_DOCUMENTS = 10;
export const MAX_COUNT = 10;
export const MAX_FOCUS = 300;
const MAX_EXISTING = 50;

export const GENERATABLE: { value: GeneratableType; label: string }[] = [
  { value: 'multiple_choice', label: 'Multiple choice' },
  { value: 'short_answer', label: 'Short answer' },
  { value: 'free_response', label: 'Written response' },
];

/* what questions are generated from: the classroom's indexed teaching materials. */
export function sourceDocuments(documents: readonly DocumentRecord[]): DocumentRecord[] {
  return documents.filter((d) => d.doc_type === 'teaching_material' && d.status === 'indexed');
}

/* why the agent's checks refused drafts, in the teacher's words. */
const DROPPED: Record<string, string> = {
  EVIDENCE_NOT_FOUND: 'its supporting quote was not in your materials',
  UNKNOWN_SOURCE: 'it cited a passage it was not given',
  INVALID_KEY: 'its answer key did not fit its options',
  INVALID_RUBRIC: 'its rubric could not be used',
  INVALID_GUIDANCE: 'its marking guidance could not be used',
  INVALID_PROMPT: 'its question could not be used',
  WRONG_TYPE: 'it was a type you did not ask for',
  DUPLICATE: 'it repeated another question',
  NOT_ANSWERABLE: 'your materials did not contain its answer',
  WRONG_KEY: 'the reviewer worked out a different answer',
  AMBIGUOUS: 'more than one answer could be right',
  WEAK_DISTRACTORS: 'its wrong options were too obvious',
  LONG_KEY: 'its answer was much longer than the other options',
  REPEATS_EXISTING: 'it asked the same thing as another question',
  NEEDS_MATERIALS: 'it only made sense with your materials open',
  NOT_REVIEWED: 'it could not be reviewed',
  UNREADABLE_REPLY: "the model's reply could not be read",
};

/* "2 drafts were left out: 1 because …; 1 because …", or '' when none were. */
export function droppedSummary(dropped: Record<string, number>): string {
  const reasons = Object.entries(dropped).filter(([, n]) => n > 0);
  const total = reasons.reduce((sum, [, n]) => sum + n, 0);
  if (!total) return '';
  const parts = reasons.map(([code, n]) => `${n} because ${DROPPED[code] ?? 'it failed a check'}`);
  return `${total} ${total === 1 ? 'draft was' : 'drafts were'} left out: ${parts.join('; ')}.`;
}

/* one line of the "how the drafts were checked" disclosure. */
export function roundSummary(round: GenerateRound, index: number): string {
  const reasons = Object.entries(round.rejected).filter(([, n]) => n > 0);
  const sent = round.sent_back ?? {};
  const parts = reasons.flatMap(([code, n]) => {
    const why = DROPPED[code] ?? 'it failed a check';
    if (code === 'NOT_REVIEWED') return [`${n} got no verdict from the reviewer and was kept`];
    const back = round.retried ? Math.min(sent[code] ?? 0, n) : 0;
    const flagged = !round.retried && (code === 'WEAK_DISTRACTORS' || code === 'LONG_KEY');
    return [
      ...(back ? [`${back} sent back to improve because ${why}`] : []),
      ...(n > back ? [`${n - back} ${flagged ? 'flagged' : 'left out'} because ${why}`] : []),
    ];
  });
  const tail = parts.length ? `; ${parts.join('; ')}` : '';
  const reviewed = round.reviewed ? `${round.passed_review} passed the reviewer` : 'not reviewed';
  return `Round ${index + 1}: ${round.generated} written, ${round.passed_checks} passed the checks, ${reviewed}${tail}.`;
}

/* what the reviewer said about a draft, or '' when there is nothing to say. */
export function reviewNote(review: GenerateReview | undefined): string {
  if (review?.status === 'flagged')
    return `${review.source === 'check' ? 'Check' : 'Reviewer'}: ${review.note ?? ''}`.trim();
  if (review?.status === 'not_reviewed' && review.note) return `Not reviewed: ${review.note}`;
  return '';
}

/* generates draft questions from the classroom's teaching materials for the teacher to review (adr 0006). */
@Component({
  selector: 'app-question-generator',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, IconComponent, SectionStateComponent],
  templateUrl: './generator.html',
})
export class QuestionGeneratorComponent implements OnChanges {
  private readonly api = inject(QuestionGenerationApi);
  private readonly usage = inject(AiUsage);
  readonly topics = inject(CurriculumNames).topics;
  @Input({ required: true }) classroomId = '';
  /* the classroom's documents, loaded once by the editor. */
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  /* the editor's question prompts, so the agent leaves out near-duplicates. */
  @Input() existingPrompts: string[] = [];
  /* whether the editor has room for another question. */
  @Input() room = true;
  @Input() disabled = false;
  /* a draft the teacher keeps, to add to the editor. */
  @Output() readonly keep = new EventEmitter<Omit<Question, 'id'>>();

  readonly types = GENERATABLE;
  readonly maxCount = MAX_COUNT;
  readonly maxFocus = MAX_FOCUS;
  readonly maxDocuments = MAX_SOURCE_DOCUMENTS;
  readonly chosen = signal<string[]>([]);
  readonly chosenTypes = signal<GeneratableType[]>(['multiple_choice', 'short_answer']);
  /* how many of each type, kept for an unticked type so it comes back as it was. */
  readonly counts = signal<Record<GeneratableType, number>>({
    multiple_choice: 3,
    short_answer: 2,
    free_response: 2,
  });
  /* the total over the ticked types; a blank or invalid box counts as 0. */
  readonly total = computed(() =>
    this.chosenTypes().reduce(
      (sum, t) => sum + (Number.isInteger(this.counts()[t]) ? this.counts()[t] : 0),
      0,
    ),
  );
  readonly totalText = computed(
    () => `${this.total()} ${this.total() === 1 ? 'question' : 'questions'} in total (up to ${MAX_COUNT}).`,
  );
  readonly topic = new FormControl('', { nonNullable: true });
  readonly difficulty = new FormControl<Difficulty>('intermediate', { nonNullable: true });
  readonly focus = new FormControl('', { nonNullable: true });
  readonly skipReview = new FormControl(false, { nonNullable: true });
  /* whether the request in flight asks for a review, fixed at send time. */
  readonly reviewing = signal(true);
  readonly result = new Section<GenerateResult>();
  /* why generating could not start, such as no document chosen. */
  readonly note = signal('');
  /* the drafts kept or discarded, by position, so only the rest are shown. */
  private readonly decided = signal<ReadonlySet<number>>(new Set());
  readonly drafts = computed(() =>
    (this.result.value()?.questions ?? [])
      .map((draft, position) => ({ draft, position }))
      .filter(({ position }) => !this.decided().has(position)),
  );
  readonly rounds = computed(() => this.result.value()?.rounds ?? []);
  readonly roundLines = computed(() => this.rounds().map(roundSummary));
  readonly modelCalls = computed(() => this.result.value()?.model_calls ?? 0);
  /* the disclosure shows when the drafts went round more than once or something was sent back. */
  readonly showRounds = computed(
    () => this.rounds().length > 1 || this.rounds().some((r) => Object.values(r.rejected).some((n) => n > 0)),
  );
  readonly dropped = computed(() => droppedSummary(this.result.value()?.dropped ?? {}));
  /* "2 of 3 questions drafted." for one type, "Drafted: Multiple choice 3 of 3 · Short answer 1 of 2." for several. */
  readonly drafted = computed(() => {
    const found = this.result.value();
    if (!found) return '';
    const asked = Object.entries(found.requested_by_type);
    if (asked.length < 2)
      return `${found.returned} of ${found.requested} ${found.requested === 1 ? 'question' : 'questions'} drafted.`;
    const parts = asked.map(
      ([type, n]) =>
        `${this.typeName(type)} ${found.questions.filter((q) => q.question.type === type).length} of ${n}`,
    );
    return `Drafted: ${parts.join(' · ')}.`;
  });

  /* another classroom's drafts and documents never carry over. */
  ngOnChanges(changes: SimpleChanges): void {
    if (!changes['classroomId'] || changes['classroomId'].firstChange) return;
    this.result.clear();
    this.chosen.set([]);
    this.decided.set(new Set());
    this.note.set('');
  }

  get choices(): DocumentRecord[] {
    return sourceDocuments(this.documents.value() ?? []);
  }

  toggleDocument(id: string, on: boolean): void {
    this.chosen.update((ids) => (on ? [...ids, id] : ids.filter((d) => d !== id)));
  }

  toggleType(type: GeneratableType, on: boolean): void {
    this.chosenTypes.update((types) => (on ? [...types, type] : types.filter((t) => t !== type)));
  }

  setCount(type: GeneratableType, value: number): void {
    this.counts.update((counts) => ({ ...counts, [type]: value }));
  }

  generate(): void {
    const documentIds = this.chosen().filter((id) => this.choices.some((d) => d.id === id));
    this.note.set(this.problem(documentIds));
    if (this.note()) return;
    this.decided.set(new Set());
    const review = !this.skipReview.value;
    this.reviewing.set(review);
    void this.result
      .load(() =>
        this.api.generate({
          classroom_id: this.classroomId,
          document_ids: documentIds,
          topic_id: this.topic.value,
          counts: Object.fromEntries(
            GENERATABLE.map((t) => t.value)
              .filter((t) => this.chosenTypes().includes(t))
              .map((t) => [t, this.counts()[t]]),
          ),
          difficulty: this.difficulty.value,
          focus: this.focus.value.trim(),
          review,
          existing_prompts: this.existingPrompts
            .map((p) => p.trim())
            .filter(Boolean)
            .slice(0, MAX_EXISTING)
            .map((p) => clipText(p, MAX_PROMPT)),
        }),
      )
      // generation can spend allowance even when it returns no questions.
      .then(() => this.usage.refresh());
  }

  /* why the request cannot be sent, or '' when it can. */
  private problem(documentIds: string[]): string {
    if (!documentIds.length) return 'Choose at least one teaching material.';
    if (documentIds.length > MAX_SOURCE_DOCUMENTS)
      return `Choose at most ${MAX_SOURCE_DOCUMENTS} teaching materials.`;
    if (!this.topic.value) return 'Choose the topic the questions assess.';
    if (!this.chosenTypes().length) return 'Choose at least one question type.';
    if (
      this.chosenTypes().some(
        (t) => !Number.isInteger(this.counts()[t]) || this.counts()[t] < 1 || this.counts()[t] > MAX_COUNT,
      )
    )
      return `Ask for 1–${MAX_COUNT} questions of each type you tick.`;
    if (this.total() > MAX_COUNT)
      return `Ask for at most ${MAX_COUNT} questions in total; you have ${this.total()}.`;
    if (this.focus.value.length > MAX_FOCUS) return `Keep the focus to ${MAX_FOCUS} characters.`;
    return '';
  }

  add(position: number): void {
    const draft = this.result.value()?.questions[position];
    if (!draft || !this.room || this.decided().has(position)) return;
    this.keep.emit(draftQuestion(draft.question));
    this.decide(position);
  }

  discard(position: number): void {
    this.decide(position);
  }

  /* hides the draft and moves focus to the next one (its add button, or discard when the assessment is full), or back to generate, so the keyboard is never lost. */
  private decide(position: number): void {
    this.decided.update((done) => new Set([...done, position]));
    const next = this.drafts().find((d) => d.position > position) ?? this.drafts()[0];
    const target = !next
      ? 'generate-button'
      : `generated-${next.position}-${this.room && !this.disabled ? 'add' : 'discard'}`;
    setTimeout(() => document.getElementById(target)?.focus());
  }

  typeName(type: string): string {
    return GENERATABLE.find((t) => t.value === type)?.label ?? type;
  }

  /* where a passage is from: its document and page. */
  source(excerpt: AgentExcerpt): string {
    return excerptLabel(fromAgent(excerpt), this.documents.value());
  }

  readonly reviewText = reviewNote;

  readonly marks = marksText;
}
