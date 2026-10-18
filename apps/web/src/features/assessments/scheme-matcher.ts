import { Component, DestroyRef, Input, inject, signal } from '@angular/core';
import { FormArray, FormControl, ReactiveFormsModule } from '@angular/forms';
import { ContentApi } from '../../api/clients';
import type { DocumentRecord } from '../../api/models';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import {
  MAX_ASSESSMENT_EXCERPTS,
  MAX_EXCERPTS,
  MAX_QUERY,
  clipText,
  schemeTotal,
  usesScheme,
  type QuestionForm,
} from './editor';
import { excerptOf, schemeDocuments } from './scheme-picker';

/** "Match scheme to questions" (ADR 0006): searches one marking scheme once per question the teacher marks and adds
 * the best passage to each, within the per-question and per-assessment limits. The teacher reviews the passages in
 * each question's marking scheme before saving; nothing is saved here. */
@Component({
  selector: 'app-scheme-matcher',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, SectionStateComponent],
  templateUrl: './scheme-matcher.html',
})
export class SchemeMatcherComponent {
  private readonly content = inject(ContentApi);
  /** Set when the editor goes away, such as on a classroom change, so a match in progress stops. */
  private gone = false;
  @Input({ required: true }) questions!: FormArray<QuestionForm>;
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  @Input() disabled = false;

  readonly document = new FormControl('', { nonNullable: true });
  readonly busy = signal(false);
  readonly message = signal('');
  readonly error = signal('');

  constructor() {
    inject(DestroyRef).onDestroy(() => (this.gone = true));
  }

  get choices(): DocumentRecord[] {
    return schemeDocuments(this.documents.value() ?? []);
  }

  async match(): Promise<void> {
    if (this.busy()) return;
    this.message.set('');
    this.error.set('');
    const documentId = this.document.value;
    if (!documentId) {
      this.error.set('Choose a marking scheme to match.');
      return;
    }
    const marked = this.questions.controls.filter((q) => usesScheme(q.controls.type.value));
    if (!marked.some((q) => q.controls.prompt.value.trim())) {
      this.error.set('Write a short-answer or written-response question first.');
      return;
    }
    this.busy.set(true);
    // Fixed for the whole match: every search and every passage belongs to the classroom it started in.
    const classroomId = this.classroomId;
    const counts = { added: 0, unmatched: 0, full: 0, already: 0 };
    try {
      for (const q of marked) {
        if (this.stopped(classroomId, q)) return;
        const prompt = clipText(q.controls.prompt.value.trim(), MAX_QUERY);
        const scheme = q.controls.marking_scheme;
        if (!prompt) continue;
        if (scheme.value.length >= MAX_EXCERPTS || this.total() >= MAX_ASSESSMENT_EXCERPTS) {
          counts.full++;
          continue;
        }
        // One at a time: the searches rerank, and the teacher's questions are few.
        const [hit] = await this.content.search({
          classroom_id: classroomId,
          query: prompt,
          document_ids: [documentId],
          top_k: 1,
        });
        if (this.stopped(classroomId, q)) return;
        if (!hit) counts.unmatched++;
        else if (scheme.value.some((e) => e.chunk_id === hit.chunk_id)) counts.already++;
        else {
          scheme.setValue([...scheme.value, excerptOf(hit)]);
          scheme.markAsDirty();
          this.questions.markAsDirty();
          counts.added++;
        }
      }
      this.message.set(summary(counts));
    } catch (error) {
      const done = counts.added ? ` ${summary(counts)}` : '';
      this.error.set(messageOf(error, 'Matching the marking scheme failed. Retry later.') + done);
    } finally {
      this.busy.set(false);
    }
  }

  /** Marking-scheme passages across the assessment's questions. */
  private total(): number {
    return schemeTotal(this.questions.getRawValue());
  }

  /** Whether the editor moved on during the match: it closed, changed classroom, or the question was removed. */
  private stopped(classroomId: string, question: QuestionForm): boolean {
    return this.gone || this.classroomId !== classroomId || !this.questions.controls.includes(question);
  }
}

/** What matching did, in the teacher's words. */
export function summary(c: { added: number; unmatched: number; full: number; already: number }): string {
  const plural = (n: number) => (n === 1 ? 'question' : 'questions');
  const parts = [
    c.added
      ? `Added a passage to ${c.added} ${plural(c.added)}. Check each one before saving.`
      : 'No passage was added.',
  ];
  if (c.already) parts.push(`${c.already} ${plural(c.already)} already had the best match.`);
  if (c.unmatched) parts.push(`Nothing in the scheme matched ${c.unmatched} ${plural(c.unmatched)}.`);
  if (c.full) parts.push(`${c.full} ${plural(c.full)} had no room for another passage.`);
  return parts.join(' ');
}
