import { Component, Input, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { QuestionGenerationApi } from '../../api/clients';
import type { DocumentRecord, Excerpt, Guidance } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import {
  MAX_ACCEPTED,
  MAX_ASSESSMENT_EXCERPTS,
  MAX_EXCERPTS,
  criterionForm,
  excerptLabel,
  levelLines,
  markWrittenBy,
  optionLines,
  type QuestionForm,
} from './editor';
import { MAX_SOURCE_DOCUMENTS, sourceDocuments } from './generator';

/** The accepted answers with the suggested ones added after them, without repeats, up to Assessment's limit. */
export function mergeAccepted(current: string, suggested: readonly string[]): string {
  const answers = optionLines(current);
  for (const answer of suggested.map((a) => a.trim()))
    if (answer && !answers.includes(answer)) answers.push(answer);
  return answers.slice(0, MAX_ACCEPTED).join('\n');
}

/** "Draft marking guidance" for a short answer or written response the teacher wrote (ADR 0006): AI drafts accepted
 * answers or a rubric, and marking points, from the classroom's documents. Nothing changes until the teacher chooses
 * "Use this", and the question is saved only with the assessment. */
@Component({
  selector: 'app-marking-guidance',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, SectionStateComponent],
  templateUrl: './guidance.html',
})
export class MarkingGuidanceComponent {
  private readonly api = inject(QuestionGenerationApi);
  private readonly usage = inject(AiUsage);
  @Input({ required: true }) question!: QuestionForm;
  /** The question's position, for unique ids and labels. */
  @Input({ required: true }) index = 0;
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  /** Marking points across the whole assessment, which has a limit of its own. */
  @Input() assessmentPassages = 0;
  @Input() disabled = false;

  /** '' drafts from the classroom's teaching materials; otherwise from the one document chosen. */
  readonly document = new FormControl('', { nonNullable: true });
  readonly result = new Section<Guidance>();
  readonly note = signal('');
  /** Whether the teacher has used the draft on show. */
  readonly used = signal(false);
  readonly levelLines = levelLines;
  /** The question as it was when the guidance on show was drafted. */
  private drafted = { prompt: '', type: '' };

  get choices(): DocumentRecord[] {
    return sourceDocuments(this.documents.value() ?? []);
  }

  draft(): void {
    this.note.set('');
    const prompt = this.question.controls.prompt.value.trim();
    const type = this.question.controls.type.value;
    if (type !== 'short_answer' && type !== 'free_response') return;
    const ids = this.document.value
      ? [this.document.value]
      : sourceDocuments(this.documents.value() ?? [])
          .slice(0, MAX_SOURCE_DOCUMENTS)
          .map((d) => d.id);
    if (!prompt) {
      this.note.set('Write the question first: the guidance is drafted for it.');
      return;
    }
    if (!ids.length) {
      this.note.set('Choose a document to draft from.');
      return;
    }
    this.used.set(false);
    this.drafted = { prompt, type };
    void this.result
      .load(() =>
        this.api.guidance({
          classroom_id: this.classroomId,
          document_ids: ids,
          prompt,
          type,
          ...(type === 'short_answer' ? { points: this.question.controls.points.value } : {}),
        }),
      )
      .then(() => this.usage.refresh());
  }

  /** Whether the question's wording or type changed since the guidance on show was drafted. */
  stale(): boolean {
    const c = this.question.controls;
    return c.prompt.value.trim() !== this.drafted.prompt || c.type.value !== this.drafted.type;
  }

  /** Puts the draft into the question: accepted answers are added, a rubric and model answer replace the old ones,
   * and the marking points are added to the question's when there is room. */
  use(): void {
    const g = this.result.value();
    const c = this.question.controls;
    if (!g || this.used() || this.stale()) return;
    if (g.type === 'short_answer') c.accepted.setValue(mergeAccepted(c.accepted.value, g.accepted_answers));
    if (g.type === 'free_response' && g.rubric) {
      c.criteria.clear();
      for (const criterion of g.rubric.criteria) c.criteria.push(criterionForm(criterion));
      c.model_answer.setValue(g.rubric.model_answer ?? '');
    }
    const scheme = c.marking_scheme.value;
    if (!this.schemeFull() && !scheme.some((e) => e.text === g.marking_points.text)) {
      markWrittenBy(c.marking_scheme, g.marking_points, 'ai');
      c.marking_scheme.setValue([...scheme, g.marking_points]);
    }
    this.question.markAsDirty();
    this.used.set(true);
    // The button goes away, so the keyboard continues from the note that replaces it.
    setTimeout(() => document.getElementById(`question-${this.index}-guidance-used`)?.focus());
  }

  /** Whether the question or the assessment has no room for another marking point. */
  schemeFull(): boolean {
    return (
      this.question.controls.marking_scheme.value.length >= MAX_EXCERPTS ||
      this.assessmentPassages >= MAX_ASSESSMENT_EXCERPTS
    );
  }

  /** Where one of the guidance's passages is from. */
  source(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.documents.value());
  }
}
