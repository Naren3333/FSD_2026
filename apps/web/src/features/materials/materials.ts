import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { Component, ElementRef, OnDestroy, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ContentApi } from '../../api/clients';
import type { DocType, DocumentRecord, FailureReason } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { documentLimit, documentMime, isIndexable } from '../../shared/files';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { rereadAfterDelete } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

const FAILURES: Record<FailureReason, string> = {
  NO_TEXT_LAYER: 'the PDF has no readable text layer',
  ENCRYPTED: 'the PDF is password protected',
  CORRUPT: 'the PDF could not be read',
  TOO_LARGE: 'the PDF is too long to index',
  AI_UNAVAILABLE: 'the AI service was unavailable after several attempts',
  INTERNAL: 'an internal error occurred',
};
/** Teachers upload teaching material only; rubrics and marking schemes uploaded before ADR 0009 keep their label
 * and stay teacher-only until deleted. */
export const DOC_TYPES: Record<DocType, string> = {
  teaching_material: 'Teaching material',
  rubric: 'Rubric',
  marking_scheme: 'Marking scheme',
};

const count = (n: number | undefined, noun: string) => `${n ?? 0} ${noun}${n === 1 ? '' : 's'}`;

/** What a document's status means for the reader; never implies text extraction is search indexing. */
export function describe(doc: DocumentRecord): { label: string; detail: string } {
  switch (doc.status) {
    case 'extracted':
      return { label: 'Text extracted', detail: 'Text files are not indexed for the assistant.' };
    case 'indexing':
      return {
        label: 'Indexing',
        detail: 'Being prepared for the assistant. This can take a few minutes for a long document.',
      };
    case 'indexed':
      return {
        label: 'Indexed',
        detail: `${doc.mime_type === 'application/pdf' ? count(doc.page_count, 'page') + ', ' : doc.mime_type.endsWith('presentation') ? count(doc.page_count, 'slide') + ', ' : ''}${count(doc.chunk_count, 'passage')}. Available to the assistant.`,
      };
    case 'failed':
      return {
        label: 'Indexing failed',
        detail: `Not available to the assistant: ${((doc.failure_reason && FAILURES[doc.failure_reason]) || 'an unknown error occurred').replace('the PDF', doc.mime_type === 'application/pdf' ? 'the PDF' : 'the document')}.`,
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
  imports: [
    NoClassroomComponent,
    PagerComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './materials.html',
})
export class MaterialsComponent implements OnDestroy {
  readonly session = inject(Session);
  private readonly content = inject(ContentApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /** Deleting a document, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  /** The document waiting for its deletion to be confirmed. */
  readonly deleting = signal<DocumentRecord | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly documents = new PagedSection<DocumentRecord>();
  readonly file = signal<File | null>(null);
  readonly polling = signal(false);
  @ViewChild('fileInput') fileInput?: ElementRef<HTMLInputElement>;
  private timer: ReturnType<typeof setTimeout> | undefined;
  readonly form = new FormGroup({
    title: new FormControl('', { nonNullable: true, validators: requiredText(160) }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => {
      this.documents.page.set(0);
      this.load();
    });
  }

  ngOnDestroy(): void {
    clearTimeout(this.timer);
  }
  load(): void {
    const id = this.classroomId();
    this.documents.clear();
    this.schedule();
    if (!id) return;
    void this.documents
      .load(() => this.documents.readPage((offset, limit) => this.content.documentPage(id, offset, limit)))
      .then(() => this.schedule());
  }
  /**
   * While a PDF is indexing, re-reads the list quietly for a teacher; a failed read stops until the next manual
   * refresh. Students don't see indexing status, so their list isn't polled.
   */
  private schedule(): void {
    clearTimeout(this.timer);
    this.polling.set(
      this.session.isTeacher() && !!this.documents.value()?.some((d) => d.status === 'indexing'),
    );
    if (!this.polling()) return;
    const id = this.classroomId();
    this.timer = setTimeout(() => {
      if (this.documents.loading() || this.state.busy() || this.deleteState.busy()) return this.schedule();
      void this.documents
        .poll(() => this.documents.readPage((offset, limit) => this.content.documentPage(id, offset, limit)))
        .then((read) => (read ? this.schedule() : this.polling.set(false)));
    }, 5000);
  }
  /** Re-reads the list after a change; it lands only while the user is still in that classroom. Indexing status keeps
   * refreshing even when this re-read fails. */
  private async reread(classroomId: string): Promise<void> {
    try {
      await this.documents.reload(() =>
        this.documents.readPage((offset, limit) => this.content.documentPage(classroomId, offset, limit)),
      );
    } finally {
      this.schedule();
    }
  }
  status(doc: DocumentRecord) {
    return describe(doc);
  }
  isPdf(doc: DocumentRecord): boolean {
    return doc.mime_type === 'application/pdf';
  }
  /** The original file in a new tab; the tab opens on the click so it isn't blocked as a pop-up. */
  view(doc: DocumentRecord): void {
    this.state.error.set('');
    const tab = window.open('', '_blank');
    this.content
      .original(doc.id)
      .then((url) => {
        if (tab) tab.location.href = url;
        else this.state.error.set('Allow pop-ups for this site to open the document.');
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      })
      .catch((error: unknown) => {
        tab?.close();
        this.state.error.set(`The document could not be opened. ${messageOf(error, '')}`.trim());
      });
  }
  typeLabel(type: DocType): string {
    return DOC_TYPES[type] ?? type;
  }
  selectFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] || null;
    if (file && (file.size === 0 || !documentMime(file) || file.size > documentLimit(file))) {
      this.state.error.set('Choose a nonempty PDF, PPTX or DOCX up to 50 MiB, or Markdown/.txt up to 5 MiB.');
      this.file.set(null);
      // The picker would otherwise still name the refused file.
      input.value = '';
      return;
    }
    this.file.set(file);
    this.form.markAsDirty();
  }
  upload(): void {
    if (!this.state.validate(this.form, 'upload-form')) return;
    const file = this.file();
    if (!file) {
      this.state.error.set('Choose a file to upload.');
      return;
    }
    const indexed = isIndexable(file);
    const { title } = this.form.getRawValue();
    const classroomId = this.classroomId();
    void this.state.run(
      async () => {
        await this.content.upload(classroomId, title, file);
        this.form.reset();
        this.file.set(null);
        if (this.fileInput) this.fileInput.nativeElement.value = '';
        await this.reread(classroomId);
      },
      indexed
        ? 'Document uploaded. Indexing has started.'
        : 'Document uploaded. Text files are not indexed for the assistant.',
    );
  }
  reindex(doc: DocumentRecord): void {
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      await this.content.reindex(doc.id);
      await this.reread(classroomId);
    }, 'Indexing has started.');
  }
  confirmDelete(doc: DocumentRecord): void {
    this.deleting.set(doc);
    this.deleteDialog?.show();
  }
  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
  }
  /** Deletes the document for good. The keyboard goes back to the library's heading, since its row is gone. */
  remove(): void {
    const doc = this.deleting();
    const classroomId = this.classroomId();
    if (!doc) return;
    void this.deleteState.run(async () => {
      await this.content.remove(doc.id);
      this.closeDelete();
      this.state.error.set('');
      this.state.message.set(`${doc.title} deleted.`);
      setTimeout(() => document.getElementById('library-title')?.focus());
      // The dialog is closed, so a failure is reported on the page, without the deleted document.
      await rereadAfterDelete(
        this.documents,
        doc.id,
        () => this.reread(classroomId),
        () => this.classroomId() === classroomId,
        this.state,
        { list: 'library', refresh: 'Refresh materials' },
      );
      // Whether to keep polling, for the list on show now that it leaves the deleted document out.
      this.schedule();
    });
  }
}
