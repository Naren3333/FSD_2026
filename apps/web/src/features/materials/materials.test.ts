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
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: { isTeacher: signal(role === 'teacher'), isStudent: signal(role === 'student') },
      },
      { provide: ContentApi, useValue: { documents: () => documents, original } },
      { provide: ClassroomContext, useValue: { classroomId: signal(classroomId) } },
    ],
  });
  TestBed.overrideComponent(MaterialsComponent, { set: { template: template('./materials.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
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
