import { Component, ElementRef, OnDestroy, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ContentApi } from '../../api/clients';
import type { DocType, DocumentRecord, FailureReason } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { isPdf } from '../../shared/files';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

const MIB = 1024 * 1024;
export const LIMITS = { pdf: 50 * MIB, txt: 5 * MIB };
const FAILURES: Record<FailureReason, string> = {
  NO_TEXT_LAYER: 'the PDF has no readable text layer',
  ENCRYPTED: 'the PDF is password protected',
  CORRUPT: 'the PDF could not be read',
  AI_UNAVAILABLE: 'the AI service was unavailable after several attempts',
  INTERNAL: 'an internal error occurred',
};
export const DOC_TYPES: { value: DocType; label: string }[] = [
  { value: 'teaching_material', label: 'Teaching material' },
  { value: 'rubric', label: 'Rubric' },
  { value: 'marking_scheme', label: 'Marking scheme' },
];

const count = (n: number | undefined, noun: string) => `${n ?? 0} ${noun}${n === 1 ? '' : 's'}`;

/** What a document's status means for the reader; never implies text extraction is search indexing. */
export function describe(doc: DocumentRecord): { label: string; detail: string } {
  switch (doc.status) {
    case 'extracted':
      return { label: 'Text extracted', detail: 'Text files are not indexed for the assistant.' };
    case 'indexing':
      return {
        label: 'Indexing',
        detail: 'Being prepared for the assistant. This can take a few minutes for a long PDF.',
      };
    case 'indexed':
      return {
        label: 'Indexed',
        detail: `${count(doc.page_count, 'page')}, ${count(doc.chunk_count, 'passage')}. Available to the assistant.`,
      };
    case 'failed':
      return {
        label: 'Indexing failed',
        detail: `Not available to the assistant: ${(doc.failure_reason && FAILURES[doc.failure_reason]) || 'an unknown error occurred'}.`,
      };
    default:
      return {
        label: 'Not indexed',
        detail: 'Uploaded before indexing was available. Index it to use it with the assistant.',
      };
  }
}

@Component({
  selector: 'app-materials',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent, SectionStateComponent],
  templateUrl: './materials.html',
})
export class MaterialsComponent implements OnDestroy {
  readonly session = inject(Session);
  private readonly content = inject(ContentApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly documents = new Section<DocumentRecord[]>();
  readonly file = signal<File | null>(null);
  readonly polling = signal(false);
  readonly types = DOC_TYPES;
  @ViewChild('fileInput') fileInput?: ElementRef<HTMLInputElement>;
  private timer: ReturnType<typeof setTimeout> | undefined;
  readonly form = new FormGroup({
    title: new FormControl('', { nonNullable: true, validators: requiredText(160) }),
    doc_type: new FormControl<DocType>('teaching_material', { nonNullable: true }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => this.load());
  }

  ngOnDestroy(): void {
    clearTimeout(this.timer);
  }
  load(): void {
    const id = this.classroomId();
    this.documents.clear();
    this.schedule();
    if (!id) return;
    void this.documents.load(() => this.content.documents(id)).then(() => this.schedule());
  }
  /** While a PDF is indexing, re-reads the list quietly; a failed read stops until the next manual refresh. */
  private schedule(): void {
    clearTimeout(this.timer);
    this.polling.set(!!this.documents.value()?.some((d) => d.status === 'indexing'));
    if (!this.polling()) return;
    const id = this.classroomId();
    this.timer = setTimeout(() => {
      if (this.documents.loading() || this.state.busy()) return this.schedule();
      void this.documents
        .poll(() => this.content.documents(id))
        .then((read) => (read ? this.schedule() : this.polling.set(false)));
    }, 5000);
  }
  private show(docs: DocumentRecord[]): void {
    this.documents.set(docs);
    this.schedule();
  }
  status(doc: DocumentRecord) {
    return describe(doc);
  }
  typeLabel(type: DocType): string {
    return DOC_TYPES.find((t) => t.value === type)?.label || type;
  }
  selectFile(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0] || null;
    const pdf = !!file && isPdf(file);
    if (
      file &&
      (file.size === 0 || !/\.(txt|pdf)$/i.test(file.name) || file.size > (pdf ? LIMITS.pdf : LIMITS.txt))
    ) {
      this.state.error.set('Choose a nonempty PDF up to 50 MiB or .txt file up to 5 MiB.');
      this.file.set(null);
      return;
    }
    this.file.set(file);
    this.form.markAsDirty();
  }
  upload(): void {
    if (!this.state.validate(this.form)) return;
    const file = this.file();
    if (!file) {
      this.state.error.set('Choose a file to upload.');
      return;
    }
    const pdf = isPdf(file);
    const { title, doc_type } = this.form.getRawValue();
    void this.state.run(
      async () => {
        await this.content.upload(this.classroomId(), title, doc_type, file);
        this.form.reset();
        this.file.set(null);
        if (this.fileInput) this.fileInput.nativeElement.value = '';
        this.show(await this.content.documents(this.classroomId()));
      },
      pdf
        ? 'Document uploaded. Indexing has started.'
        : 'Document uploaded. Text files are not indexed for the assistant.',
    );
  }
  reindex(doc: DocumentRecord): void {
    void this.state.run(async () => {
      await this.content.reindex(doc.id);
      this.show(await this.content.documents(this.classroomId()));
    }, 'Indexing has started.');
  }
}
