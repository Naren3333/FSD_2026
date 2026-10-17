import { Component, Input, computed, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { ContentApi } from '../../api/clients';
import type { DocumentRecord, Excerpt, SearchHit } from '../../api/models';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { MAX_EXCERPTS, MAX_EXCERPT_TEXT, characters, clipText } from './editor';

/** How many passages one search offers. */
const SEARCH_RESULTS = 5;
/** The longest search query Content takes. */
const MAX_QUERY = 1000;

/** The classroom's documents a marking-scheme passage can come from: indexed rubrics and marking schemes. */
export function schemeDocuments(documents: readonly DocumentRecord[]): DocumentRecord[] {
  return documents.filter((d) => d.doc_type !== 'teaching_material' && d.status === 'indexed');
}

/** A search hit as a passage on the question, cut to Assessment's limit. */
export function excerptOf(hit: SearchHit): Excerpt {
  return {
    document_id: hit.document_id,
    chunk_id: hit.chunk_id,
    page: hit.page_start,
    text: clipText(hit.content, MAX_EXCERPT_TEXT),
  };
}

/** A question's marking-scheme passages (ADR 0005): the teacher searches one of the classroom's marking schemes for
 * passages about the question and keeps the ones that apply, or pastes a passage. AI marking suggestions read them
 * with the rubric; students never see them. */
@Component({
  selector: 'app-scheme-picker',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, SectionStateComponent],
  templateUrl: './scheme-picker.html',
})
export class SchemePickerComponent {
  private readonly content = inject(ContentApi);
  /** The question's passages. */
  @Input({ required: true }) control!: FormControl<Excerpt[]>;
  /** The question's position, for unique ids and labels. */
  @Input({ required: true }) index = 0;
  /** The question's wording, which the search looks for. */
  @Input({ required: true }) prompt = '';
  @Input({ required: true }) classroomId = '';
  /** The classroom's documents, loaded once by the editor for every question. */
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  @Input() disabled = false;

  readonly maxExcerpts = MAX_EXCERPTS;
  readonly maxText = MAX_EXCERPT_TEXT;
  readonly document = new FormControl('', { nonNullable: true });
  readonly pasted = new FormControl('', { nonNullable: true });
  readonly results = new Section<SearchHit[]>();
  /** Why a search could not start, such as a blank question. */
  readonly searchNote = signal('');
  /** Bumped on every change to the passages, so the template's reads stay current. */
  private readonly changes = signal(0);
  readonly excerpts = computed(() => (this.changes(), this.control.value));
  readonly full = computed(() => this.excerpts().length >= MAX_EXCERPTS);

  get choices(): DocumentRecord[] {
    return schemeDocuments(this.documents.value() ?? []);
  }

  /** Searches the chosen document for passages about the question. */
  find(): void {
    this.searchNote.set('');
    const query = clipText(this.prompt.trim(), MAX_QUERY);
    const documentId = this.document.value;
    if (!documentId) {
      this.searchNote.set('Choose a marking scheme to search.');
      return;
    }
    if (!query) {
      this.searchNote.set('Write the question first: the search looks for passages about it.');
      return;
    }
    void this.results.load(() =>
      this.content.search({
        classroom_id: this.classroomId,
        query,
        document_ids: [documentId],
        top_k: SEARCH_RESULTS,
      }),
    );
  }

  /** Whether the hit is already one of the question's passages. */
  added(hit: SearchHit): boolean {
    return this.excerpts().some((e) => e.chunk_id === hit.chunk_id);
  }

  add(hit: SearchHit): void {
    if (this.full() || this.added(hit)) return;
    this.set([...this.control.value, excerptOf(hit)]);
  }

  addPasted(): void {
    const text = this.pasted.value.trim();
    if (!text || this.full() || characters(text) > MAX_EXCERPT_TEXT) return;
    this.set([...this.control.value, { text }]);
    this.pasted.reset();
  }

  remove(position: number): void {
    this.set(this.control.value.filter((_, i) => i !== position));
  }

  /** Where a passage is from: its document and page, or that the teacher pasted it. */
  source(excerpt: Excerpt): string {
    if (!excerpt.document_id) return 'Pasted passage';
    const title =
      this.documents.value()?.find((d) => d.id === excerpt.document_id)?.title ?? 'Marking scheme';
    return excerpt.page ? `${title}, page ${excerpt.page}` : title;
  }

  pages(hit: SearchHit): string {
    return hit.page_end > hit.page_start
      ? `pages ${hit.page_start}–${hit.page_end}`
      : `page ${hit.page_start}`;
  }

  pastedLength(): number {
    return characters(this.pasted.value);
  }

  private set(excerpts: Excerpt[]): void {
    this.control.setValue(excerpts);
    this.control.markAsDirty();
    this.changes.update((n) => n + 1);
  }
}
