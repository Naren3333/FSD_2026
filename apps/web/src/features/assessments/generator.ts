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
  Excerpt,
  GenerateResult,
  GeneratableType,
  Question,
} from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { CurriculumNames } from '../../core/curriculum-names';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { MAX_PROMPT, clipText, excerptLabel, marksText } from './editor';

/** The agent's limits (ADR 0006). */
export const MAX_SOURCE_DOCUMENTS = 10;
export const MAX_COUNT = 10;
export const MAX_FOCUS = 300;
const MAX_EXISTING = 50;

export const GENERATABLE: { value: GeneratableType; label: string }[] = [
  { value: 'multiple_choice', label: 'Multiple choice' },
  { value: 'short_answer', label: 'Short answer' },
  { value: 'free_response', label: 'Written response' },
];

/** What questions are generated from: the classroom's indexed teaching materials. */
export function sourceDocuments(documents: readonly DocumentRecord[]): DocumentRecord[] {
  return documents.filter((d) => d.doc_type === 'teaching_material' && d.status === 'indexed');
}

/** Why the agent's checks refused drafts, in the teacher's words. */
const DROPPED: Record<string, string> = {
  EVIDENCE_NOT_FOUND: 'its supporting quote was not in your materials',
  UNKNOWN_SOURCE: 'it cited a passage it was not given',
  INVALID_KEY: 'its answer key did not fit its options',
  INVALID_RUBRIC: 'its rubric could not be used',
  INVALID_GUIDANCE: 'its marking guidance could not be used',
  INVALID_PROMPT: 'its question could not be used',
  WRONG_TYPE: 'it was a type you did not ask for',
  DUPLICATE: 'it repeated another question',
};

/** "2 drafts were left out: 1 because …; 1 because …", or '' when none were. */
export function droppedSummary(dropped: Record<string, number>): string {
  const reasons = Object.entries(dropped).filter(([, n]) => n > 0);
  const total = reasons.reduce((sum, [, n]) => sum + n, 0);
  if (!total) return '';
  const parts = reasons.map(([code, n]) => `${n} because ${DROPPED[code] ?? 'it failed a check'}`);
  return `${total} ${total === 1 ? 'draft was' : 'drafts were'} left out: ${parts.join('; ')}.`;
}

/** Generates draft questions from the classroom's teaching materials for the teacher to review (ADR 0006). Nothing
 * is saved here: a kept draft goes into the editor like any question the teacher wrote, and is saved with the
 * assessment. */
@Component({
  selector: 'app-question-generator',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, SectionStateComponent],
  templateUrl: './generator.html',
})
export class QuestionGeneratorComponent implements OnChanges {
  private readonly api = inject(QuestionGenerationApi);
  private readonly usage = inject(AiUsage);
  readonly topics = inject(CurriculumNames).topics;
  @Input({ required: true }) classroomId = '';
  /** The classroom's documents, loaded once by the editor. */
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  /** The editor's question prompts, so the agent leaves out near-duplicates. */
  @Input() existingPrompts: string[] = [];
  /** Whether the editor has room for another question. */
  @Input() room = true;
  @Input() disabled = false;
  /** A draft the teacher keeps, to add to the editor. */
  @Output() readonly keep = new EventEmitter<Omit<Question, 'id'>>();

  readonly types = GENERATABLE;
  readonly maxCount = MAX_COUNT;
  readonly maxFocus = MAX_FOCUS;
  readonly maxDocuments = MAX_SOURCE_DOCUMENTS;
  readonly chosen = signal<string[]>([]);
  readonly chosenTypes = signal<GeneratableType[]>(['multiple_choice', 'short_answer']);
  readonly topic = new FormControl('', { nonNullable: true });
  readonly difficulty = new FormControl<Difficulty>('intermediate', { nonNullable: true });
  readonly count = new FormControl(5, { nonNullable: true });
  readonly focus = new FormControl('', { nonNullable: true });
  readonly result = new Section<GenerateResult>();
  /** Why generating could not start, such as no document chosen. */
  readonly note = signal('');
  /** The drafts kept or discarded, by position, so only the rest are shown. */
  private readonly decided = signal<ReadonlySet<number>>(new Set());
  readonly drafts = computed(() =>
    (this.result.value()?.questions ?? [])
      .map((draft, position) => ({ draft, position }))
      .filter(({ position }) => !this.decided().has(position)),
  );
  readonly dropped = computed(() => droppedSummary(this.result.value()?.dropped ?? {}));

  /** Another classroom's drafts and documents never carry over. */
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

  generate(): void {
    const count = this.count.value;
    const documentIds = this.chosen().filter((id) => this.choices.some((d) => d.id === id));
    this.note.set(this.problem(documentIds, count));
    if (this.note()) return;
    this.decided.set(new Set());
    void this.result
      .load(() =>
        this.api.generate({
          classroom_id: this.classroomId,
          document_ids: documentIds,
          topic_id: this.topic.value,
          question_types: GENERATABLE.map((t) => t.value).filter((t) => this.chosenTypes().includes(t)),
          difficulty: this.difficulty.value,
          count,
          focus: this.focus.value.trim(),
          existing_prompts: this.existingPrompts
            .map((p) => p.trim())
            .filter(Boolean)
            .slice(0, MAX_EXISTING)
            .map((p) => clipText(p, MAX_PROMPT)),
        }),
      )
      // Generating spends the teacher's daily allowance, whether or not it found anything.
      .then(() => this.usage.refresh());
  }

  /** Why the request cannot be sent, or '' when it can. */
  private problem(documentIds: string[], count: number): string {
    if (!documentIds.length) return 'Choose at least one teaching material.';
    if (documentIds.length > MAX_SOURCE_DOCUMENTS)
      return `Choose at most ${MAX_SOURCE_DOCUMENTS} teaching materials.`;
    if (!this.topic.value) return 'Choose the topic the questions assess.';
    if (!this.chosenTypes().length) return 'Choose at least one question type.';
    if (!Number.isInteger(count) || count < 1 || count > MAX_COUNT)
      return `Ask for 1–${MAX_COUNT} questions.`;
    if (this.focus.value.length > MAX_FOCUS) return `Keep the focus to ${MAX_FOCUS} characters.`;
    return '';
  }

  add(position: number): void {
    const draft = this.result.value()?.questions[position];
    if (!draft || !this.room || this.decided().has(position)) return;
    this.keep.emit(draft.question);
    this.decide(position);
  }

  discard(position: number): void {
    this.decide(position);
  }

  /** Hides the draft and moves focus to the next one (its Add button, or Discard when the assessment is full), or
   * back to Generate, so the keyboard is never lost. */
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

  /** Where a passage is from: its document and page. */
  source(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.documents.value());
  }

  readonly marks = marksText;
}
