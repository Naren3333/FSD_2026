// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
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
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: { isTeacher: signal(role === 'teacher'), isStudent: signal(role === 'student') },
      },
      { provide: ContentApi, useValue: { documents: () => documents } },
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
    expect(student.library()).toContain('Indexing failed');
    expect(student.library()).not.toContain('Retry indexing');
    expect(student.root.textContent).not.toContain('Upload a document');
  });
});
