// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { ContentApi } from '../../api/clients';
import type { DocumentRecord } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { MaterialsComponent } from './materials';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: 'd1',
  classroom_id: 'c1',
  title: 'Fractions notes',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  doc_type: 'teaching_material',
  page_count: 3,
  chunk_count: 5,
  ...change,
});

/** The materials page for a user with `role` in classroom `classroomId`, whose documents load as given. */
function render(
  documents: Promise<DocumentRecord[]>,
  role: 'teacher' | 'student' = 'teacher',
  classroomId = 'c1',
  original: (id: string) => Promise<string> = () => Promise.resolve('blob:pdf'),
  remove: (id: string) => Promise<void> = () => Promise.resolve(),
  classroomsKnown = true,
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: { isTeacher: signal(role === 'teacher'), isStudent: signal(role === 'student') },
      },
      { provide: ContentApi, useValue: { documents: () => documents, original, remove } },
      {
        provide: ClassroomContext,
        useValue: {
          classroomId: signal(classroomId),
          known: signal(classroomsKnown),
          loading: signal(!classroomsKnown),
          error: signal(''),
        },
      },
    ],
  });
  TestBed.overrideComponent(MaterialsComponent, { set: { template: template('./materials.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(MaterialsComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const library = () => root.querySelector('section.panel')?.textContent ?? '';
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { root, library, settle, fixture };
}

describe('materials states', () => {
  it('shows loading, not the empty text, until the list arrives', async () => {
    const page = render(new Promise(() => undefined));
    await page.settle();
    expect(page.library()).toContain('Loading materials…');
    expect(page.library()).not.toContain('No documents in this classroom');
  });

  it('explains an empty library and the next step', async () => {
    const page = render(Promise.resolve([]));
    await page.settle();
    expect(page.library()).toContain('No documents in this classroom. Upload a teaching resource to begin.');
  });

  it('asks for a classroom when none is chosen', async () => {
    const page = render(Promise.resolve([]), 'teacher', '');
    await page.settle();
    expect(page.library()).toContain('Select a classroom to view its materials.');
  });

  it('says the classrooms are loading, not that none is chosen, until they are known', async () => {
    const page = render(Promise.resolve([]), 'teacher', '', undefined, undefined, false);
    await page.settle();
    expect(page.library()).toContain('Loading your classrooms…');
    expect(page.library()).not.toContain('Select a classroom');
  });

  it('shows the failure, not the empty text, when the list fails', async () => {
    const page = render(Promise.reject(new Error('Content is unavailable.')));
    await page.settle();
    expect(page.library()).toContain('Content is unavailable.');
    expect(page.library()).not.toContain('No documents in this classroom');
  });

  it('shows each document’s indexing status, with a retry for teachers only', async () => {
    const failed = doc({
      id: 'd2',
      title: 'Scanned worksheet',
      status: 'failed',
      failure_reason: 'NO_TEXT_LAYER',
    });
    const teacher = render(Promise.resolve([doc({}), failed]));
    await teacher.settle();
    expect(teacher.library()).toContain('3 pages, 5 passages. Available to the assistant.');
    expect(teacher.library()).toContain('Indexing failed');
    expect(teacher.library()).toContain('the PDF has no readable text layer');
    expect(teacher.library()).toContain('Retry indexing');
    teacher.fixture.destroy();

    const student = render(Promise.resolve([doc({}), failed]), 'student');
    await student.settle();
    expect(student.library()).toContain('Scanned worksheet');
    expect(student.library()).not.toContain('Retry indexing');
    expect(student.root.textContent).not.toContain('Upload a document');
  });

  it('hides indexing details from students', async () => {
    const page = render(Promise.resolve([doc({}), doc({ id: 'd2', status: 'indexing' })]), 'student');
    await page.settle();
    const text = page.root.textContent ?? '';
    for (const hidden of ['Indexed', 'Indexing', 'passage', 'pages', 'Revision', 'application/pdf'])
      expect(text).not.toContain(hidden);
    expect(page.library()).toContain('Fractions notes');
    expect(page.fixture.componentInstance.polling()).toBe(false);
    page.fixture.destroy();
  });

  it('tells a student when nothing has been shared', async () => {
    const page = render(Promise.resolve([]), 'student');
    await page.settle();
    expect(page.library()).toContain('No materials have been shared in this classroom yet.');
  });
});

describe('viewing a material', () => {
  afterEach(() => vi.restoreAllMocks());

  for (const role of ['teacher', 'student'] as const)
    it(`opens the original PDF in a new tab for a ${role}`, async () => {
      const tab = { location: { href: '' }, close: vi.fn() };
      vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
      const original = vi.fn(() => Promise.resolve('blob:fractions'));
      const page = render(Promise.resolve([doc({})]), role, 'c1', original);
      await page.settle();
      const button = [...page.root.querySelectorAll('button')].find((b) =>
        b.textContent?.includes('View PDF'),
      );
      button?.click();
      await page.settle();
      expect(original).toHaveBeenCalledWith('d1');
      expect(tab.location.href).toBe('blob:fractions');
    });

  it('closes the tab and explains when the PDF cannot be opened', async () => {
    const tab = { location: { href: '' }, close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(tab as unknown as Window);
    const page = render(Promise.resolve([doc({})]), 'student', 'c1', () =>
      Promise.reject(new Error('Document not found.')),
    );
    await page.settle();
    page.fixture.componentInstance.view(doc({}));
    await page.settle();
    expect(tab.close).toHaveBeenCalled();
    expect(page.root.textContent).toContain('The document could not be opened. Document not found.');
  });
});

describe('deleting a material', () => {
  // jsdom has no modal dialogs: these do what a browser's do for the parts the dialog relies on.
  beforeAll(() => {
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    };
    HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
      if (!this.open) return;
      this.removeAttribute('open');
      this.dispatchEvent(new Event('close'));
    };
  });
  const buttons = (root: HTMLElement, text: string) =>
    [...root.querySelectorAll('article button')].filter((b) => b.textContent?.trim().startsWith(text));

  it('offers Delete to teachers only, and labels a retired marking scheme', async () => {
    const scheme = doc({ id: 'd2', title: 'Old mark scheme', doc_type: 'marking_scheme' });
    const teacher = render(Promise.resolve([doc({}), scheme]));
    await teacher.settle();
    expect(buttons(teacher.root, 'Delete').map((b) => b.textContent?.trim())).toEqual([
      'Delete Fractions notes',
      'Delete Old mark scheme',
    ]);
    expect(teacher.library()).toContain('Marking scheme');
    expect(teacher.library()).toContain('Uploaded before marking schemes were retired.');
    expect(teacher.library()).not.toContain('Teaching material');
    expect(teacher.root.querySelector('#document-type')).toBeNull();
    teacher.fixture.destroy();

    const student = render(Promise.resolve([doc({})]), 'student');
    await student.settle();
    expect(buttons(student.root, 'Delete')).toEqual([]);
  });

  it('deletes the confirmed document and re-reads the list', async () => {
    const remove = vi.fn(() => Promise.resolve());
    let listed = [doc({}), doc({ id: 'd2', title: 'Old notes' })];
    const page = render(Promise.resolve(listed), 'teacher', 'c1', undefined, remove);
    await page.settle();
    const component = page.fixture.componentInstance;
    const content = TestBed.inject(ContentApi) as unknown as { documents: () => Promise<DocumentRecord[]> };
    content.documents = () => Promise.resolve(listed);
    (buttons(page.root, 'Delete')[1] as HTMLButtonElement).click();
    page.fixture.detectChanges();
    const dialog = page.root.querySelector('dialog');
    expect(dialog?.open).toBe(true);
    expect(dialog?.textContent).toContain('Old notes is permanently deleted');
    listed = [listed[0]];
    [...(dialog?.querySelectorAll('button') ?? [])]
      .find((b) => b.textContent?.includes('Delete document'))
      ?.click();
    await vi.waitFor(() => expect(component.state.message()).toBe('Old notes deleted.'));
    await page.settle();
    expect(remove).toHaveBeenCalledWith('d2');
    expect(dialog?.open).toBe(false);
    expect(component.deleting()).toBeNull();
    expect(page.library()).not.toContain('Old notes');
  });

  it('announces the deletion and says on the page when the list cannot be re-read', async () => {
    const remove = vi.fn(() => Promise.resolve());
    const page = render(
      Promise.resolve([doc({}), doc({ id: 'd2', title: 'Old notes' })]),
      'teacher',
      'c1',
      undefined,
      remove,
    );
    await page.settle();
    const component = page.fixture.componentInstance;
    const content = TestBed.inject(ContentApi) as unknown as { documents: () => Promise<DocumentRecord[]> };
    content.documents = () => Promise.reject(new Error('Content is unavailable.'));
    component.confirmDelete(doc({ id: 'd2', title: 'Old notes' }));
    component.remove();
    await vi.waitFor(() =>
      expect(component.state.error()).toBe('The library could not be refreshed. Content is unavailable.'),
    );
    await page.settle();
    expect(component.state.message()).toBe('Old notes deleted.');
    expect(component.deleteState.error()).toBe('');
    expect(page.root.querySelector('dialog')?.open).toBe(false);
    const status = page.root.querySelector('app-status')?.textContent ?? '';
    expect(status).toContain('Old notes deleted.');
    expect(status).toContain('The library could not be refreshed. Content is unavailable.');
    expect(page.library()).toContain('Fractions notes');
    expect(page.library()).not.toContain('Old notes');
  });

  it('keeps refreshing indexing status when the list cannot be re-read after a delete', async () => {
    const indexing = doc({ id: 'd3', title: 'New notes', status: 'indexing' });
    const page = render(Promise.resolve([indexing, doc({ id: 'd2', title: 'Old notes' })]));
    await page.settle();
    const component = page.fixture.componentInstance;
    expect(component.polling()).toBe(true);
    const content = TestBed.inject(ContentApi) as unknown as { documents: () => Promise<DocumentRecord[]> };
    content.documents = () => Promise.reject(new Error('Content is unavailable.'));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      component.confirmDelete(doc({ id: 'd2', title: 'Old notes' }));
      component.remove();
      await vi.waitFor(() =>
        expect(component.state.error()).toContain('The library could not be refreshed.'),
      );
      const poll = vi.fn(() => Promise.resolve([{ ...indexing, status: 'indexed' as const }]));
      content.documents = poll;
      await vi.advanceTimersByTimeAsync(5000);
      expect(poll).toHaveBeenCalled();
      expect(component.polling()).toBe(false);
    } finally {
      vi.useRealTimers();
      page.fixture.destroy();
    }
  });

  it('keeps the document and explains when the delete fails', async () => {
    const remove = vi.fn(() => Promise.reject(new Error('Document not found.')));
    const page = render(Promise.resolve([doc({})]), 'teacher', 'c1', undefined, remove);
    await page.settle();
    const component = page.fixture.componentInstance;
    component.confirmDelete(doc({}));
    component.remove();
    await vi.waitFor(() => expect(component.deleteState.error()).toContain('Document not found.'));
    page.fixture.detectChanges();
    expect(component.deleting()).not.toBeNull();
    expect(page.root.querySelector('dialog')?.textContent).toContain('Document not found.');
    expect(page.library()).toContain('Fractions notes');
  });
});
