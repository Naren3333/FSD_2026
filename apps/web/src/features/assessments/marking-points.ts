import { Component, DestroyRef, Input, OnInit, computed, inject, signal } from '@angular/core';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import type { DocumentRecord, Excerpt } from '../../api/models';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { MAX_EXCERPTS, MAX_EXCERPT_TEXT, characters, excerptLabel, markWrittenBy } from './editor';

/* a question's marking points: short notes the teacher writes (or keeps from ai) on what earns the marks. */
@Component({
  selector: 'app-marking-points',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective],
  templateUrl: './marking-points.html',
})
export class MarkingPointsComponent implements OnInit {
  private readonly destroyRef = inject(DestroyRef);
  /* the question's marking points. */
  @Input({ required: true }) control!: FormControl<Excerpt[]>;
  /* the question's position, for unique ids and labels. */
  @Input({ required: true }) index = 0;
  /* whether the question is a free response (marked with a rubric) rather than a short answer. */
  @Input() freeResponse = false;
  /* the classroom's documents, to name the one a point copied before adr 0009 came from. */
  @Input() documents: readonly DocumentRecord[] | null = null;
  @Input() disabled = false;

  readonly maxExcerpts = MAX_EXCERPTS;
  readonly maxText = MAX_EXCERPT_TEXT;
  readonly written = new FormControl('', { nonNullable: true });
  /* bumped on every change to the points, so the template's reads stay current. */
  private readonly changes = signal(0);
  readonly excerpts = computed(() => (this.changes(), this.control.value));
  readonly full = computed(() => this.excerpts().length >= MAX_EXCERPTS);

  ngOnInit(): void {
    // drafted guidance and kept generated questions add points from outside.
    const watch = this.control.valueChanges.subscribe(() => this.changes.update((n) => n + 1));
    // a point written but not yet added is unsaved work too: saving leaves it out, so the editor warns
    // about it.
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

  /* who wrote a point: the teacher or ai, or the document an older one was copied from. */
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
