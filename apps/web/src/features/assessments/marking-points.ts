import { Component, DestroyRef, Input, OnInit, computed, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import type { DocumentRecord, Excerpt } from '../../api/models';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { MAX_EXCERPTS, MAX_EXCERPT_TEXT, characters, excerptLabel, markWrittenBy } from './editor';

/** A question's marking points: short notes the teacher writes (or keeps from AI) on what earns the marks. AI
 * marking suggestions read them with the rubric or accepted answers; students never see them. They travel as the
 * question's `marking_scheme` passages (ADR 0005), which no longer come from uploaded marking schemes (ADR 0009). */
@Component({
  selector: 'app-marking-points',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective],
  templateUrl: './marking-points.html',
})
export class MarkingPointsComponent implements OnInit {
  private readonly destroyRef = inject(DestroyRef);
  /** The question's marking points. */
  @Input({ required: true }) control!: FormControl<Excerpt[]>;
  /** The question's position, for unique ids and labels. */
  @Input({ required: true }) index = 0;
  /** Whether the question is a free response (marked with a rubric) rather than a short answer. */
  @Input() freeResponse = false;
  /** The classroom's documents, to name the one a point copied before ADR 0009 came from. */
  @Input() documents: readonly DocumentRecord[] | null = null;
  @Input() disabled = false;

  readonly maxExcerpts = MAX_EXCERPTS;
  readonly maxText = MAX_EXCERPT_TEXT;
  readonly written = new FormControl('', { nonNullable: true });
  /** Bumped on every change to the points, so the template's reads stay current. */
  private readonly changes = signal(0);
  readonly excerpts = computed(() => (this.changes(), this.control.value));
  readonly full = computed(() => this.excerpts().length >= MAX_EXCERPTS);

  ngOnInit(): void {
    // Drafted guidance and kept generated questions add points from outside.
    const watch = this.control.valueChanges.subscribe(() => this.changes.update((n) => n + 1));
    // A point written but not yet added is unsaved work too: saving leaves it out, so the editor warns about it.
    const typing = this.written.valueChanges.subscribe((text) => {
      if (text.trim()) this.control.markAsDirty();
    });
    this.destroyRef.onDestroy(() => {
      watch.unsubscribe();
      typing.unsubscribe();
    });
  }

  add(): void {
    const text = this.written.value.trim();
    if (!text || this.full() || characters(text) > MAX_EXCERPT_TEXT) return;
    markWrittenBy(this.control, { text }, 'teacher');
    this.set([...this.control.value, { text }]);
    this.written.reset();
  }

  remove(position: number): void {
    this.set(this.control.value.filter((_, i) => i !== position));
  }

  /** Who wrote a point: the teacher or AI, or the document an older one was copied from. */
  source(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.documents ?? undefined, this.control);
  }

  writtenLength(): number {
    return characters(this.written.value);
  }

  private set(excerpts: Excerpt[]): void {
    this.control.setValue(excerpts);
    this.control.markAsDirty();
  }
}
