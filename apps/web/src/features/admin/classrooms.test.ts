// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AdminApi } from '../../api/clients';
import type { Account, SchoolClassroom, Student } from '../../api/models';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { AdminClassroomsComponent } from './classrooms';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
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
afterEach(() => vi.restoreAllMocks());

const lee: Account = { id: 't1', username: 'lee', display_name: 'Lee Tan', role: 'teacher' };
const ng: Account = { id: 't2', username: 'ng', display_name: 'Ng Wei', role: 'teacher' };
const ana: Account = { id: 's1', username: 'ana', display_name: 'Ana Lim', role: 'student' };
const bo: Account = { id: 's2', username: 'bo', display_name: 'Bo Koh', role: 'student' };
const room: SchoolClassroom = {
  id: 'c1',
  name: '3A',
  teacher_id: 't1',
  teacher_name: 'Lee Tan',
  students: 1,
};

/* the admin's classrooms page over an admin api with these teachers, classrooms and one enrolled student. */
function render(teachers: Account[], rooms: SchoolClassroom[], admin: Record<string, unknown> = {}) {
  TestBed.resetTestingModule();
  const enrolled: Student[] = [{ id: 's1', username: 'ana', display_name: 'Ana Lim' }];
  const api = {
    teachers: vi.fn(() => Promise.resolve(teachers)),
    classrooms: vi.fn(() => Promise.resolve({ items: rooms, total: rooms.length })),
    createClassroom: vi.fn(() => Promise.resolve({ id: 'c9', name: 'New', teacher_id: 't1' })),
    updateClassroom: vi.fn(() => Promise.resolve({ classroom_id: 'c1' })),
    deleteClassroom: vi.fn(() => Promise.resolve({ classroom_id: 'c1' })),
    rosterPage: vi.fn(() => Promise.resolve({ students: enrolled, total: enrolled.length })),
    rosterIds: vi.fn(() => Promise.resolve(enrolled.map((s) => s.id))),
    accounts: vi.fn(() => Promise.resolve({ accounts: [ana, bo], total: 2 })),
    enroll: vi.fn(() => Promise.resolve({ student_id: 's2', classroom_id: 'c1' })),
    unenroll: vi.fn(() => Promise.resolve({ student_id: 's1', classroom_id: 'c1' })),
    ...admin,
  };
  TestBed.configureTestingModule({ providers: [provideRouter([]), { provide: AdminApi, useValue: api }] });
  TestBed.overrideComponent(AdminClassroomsComponent, { set: { template: template('./classrooms.html') } });
  for (const [component, path] of [
    [PageHeaderComponent, '../../shared/page-header.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [PagerComponent, '../../shared/pager.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(AdminClassroomsComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === name || b.getAttribute('aria-label') === name,
    ) as HTMLButtonElement;
  const panel = (id: string) => root.querySelector(`[aria-labelledby="${id}"]`)?.textContent ?? '';
  const fill = (id: string, value: string) => {
    const field = root.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement;
    field.value = value;
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input'));
    fixture.detectChanges();
  };
  return { root, fixture, settle, button, panel, fill, api, component: fixture.componentInstance };
}

describe('the school classrooms list', () => {
  it('lists each classroom with its teacher and student count', async () => {
    const page = render([lee], [room]);
    await page.settle();
    expect(page.panel('school-classrooms-title')).toContain('3A');
    expect(page.panel('school-classrooms-title')).toContain('Lee Tan');
    expect(page.root.querySelector('td[data-label="Students"]')?.textContent?.trim()).toBe('1');
    expect(page.button('Edit 3A')).toBeTruthy();
    expect(page.button('Delete 3A')).toBeTruthy();
    expect(page.root.querySelector('.page-header-subtitle')?.textContent?.trim()).toBe(
      '1 classroom in your school',
    );
  });

  it('explains an empty school, and the missing teachers', async () => {
    const page = render([], []);
    await page.settle();
    expect(page.panel('school-classrooms-title')).toContain('No classrooms yet.');
    expect(page.panel('classroom-form-title')).toContain('Your school has no teacher accounts yet.');
    expect(page.button('Create classroom').disabled).toBe(true);
  });

  it('shows the list’s failure in place', async () => {
    const page = render([lee], [], {
      classrooms: () => Promise.reject(new Error('Identity is unavailable.')),
    });
    await page.settle();
    expect(page.panel('school-classrooms-title')).toContain('Identity is unavailable.');
    expect(page.panel('school-classrooms-title')).not.toContain('No classrooms yet');
  });
});

describe('creating and editing a classroom', () => {
  it('asks for a name and a teacher before creating', async () => {
    const page = render([lee, ng], []);
    await page.settle();
    page.button('Create classroom').click();
    await page.settle();
    expect(page.api.createClassroom).not.toHaveBeenCalled();
    expect(page.root.textContent).toContain("Choose the classroom's teacher.");
    page.fill('class-name', ' 3B ');
    page.fill('class-teacher', 't2');
    page.button('Create classroom').click();
    await page.settle();
    expect(page.api.createClassroom).toHaveBeenCalledWith('3B', 't2');
    expect(page.root.textContent).toContain('Classroom created.');
    expect(page.component.form.getRawValue()).toEqual({ name: '', teacher: '' });
  });

  it('renames a classroom and gives it another teacher', async () => {
    const page = render([lee, ng], [room]);
    await page.settle();
    page.button('Edit 3A').click();
    await page.settle();
    expect(page.panel('classroom-form-title')).toContain('Edit 3A');
    expect((page.root.querySelector('#class-name') as HTMLInputElement).value).toBe('3A');
    page.fill('class-teacher', 't2');
    page.button('Save classroom').click();
    await page.settle();
    expect(page.api.updateClassroom).toHaveBeenCalledWith('c1', '3A', 't2');
    expect(page.root.textContent).toContain('Classroom saved.');
    expect(page.button('Create classroom')).toBeTruthy();
  });

  it('asks before editing another classroom discards unsaved changes', async () => {
    const other: SchoolClassroom = {
      ...room,
      id: 'c2',
      name: '4B',
      teacher_id: 't2',
      teacher_name: 'Ng Wei',
    };
    const page = render([lee, ng], [room, other]);
    await page.settle();
    page.fill('class-name', 'Year 9 Maths');
    page.button('Edit 4B').click();
    page.fixture.detectChanges();
    const dialog = page.root.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Discard your changes?');
    expect((page.root.querySelector('#class-name') as HTMLInputElement).value).toBe('Year 9 Maths');
    // cancelling keeps the changes; discarding edits the classroom chosen.
    page.component.closeDiscard();
    page.fixture.detectChanges();
    expect(page.root.querySelector('dialog[open]')).toBeNull();
    expect((page.root.querySelector('#class-name') as HTMLInputElement).value).toBe('Year 9 Maths');
    page.button('Edit 4B').click();
    page.fixture.detectChanges();
    page.button('Discard changes').click();
    await page.settle();
    expect(page.panel('classroom-form-title')).toContain('Edit 4B');
    expect((page.root.querySelector('#class-name') as HTMLInputElement).value).toBe('4B');
  });

  it('asks before deleting a classroom', async () => {
    const page = render([lee], [room]);
    await page.settle();
    page.button('Delete 3A').click();
    page.fixture.detectChanges();
    const dialog = page.root.querySelector('dialog[open]');
    expect(dialog?.textContent).toContain('Delete this classroom?');
    expect(dialog?.textContent).toContain('3A will disappear for its teacher and its students');
    page.button('Delete classroom').click();
    await page.settle();
    expect(page.api.deleteClassroom).toHaveBeenCalledWith('c1');
  });
});

describe('a classroom’s students', () => {
  it('shows the roster beside the school’s students, marking those enrolled', async () => {
    const page = render([lee], [room]);
    await page.settle();
    page.button('Manage students of 3A').click();
    await page.settle();
    expect(page.panel('admin-roster-title')).toContain('Students in 3A');
    expect(page.panel('admin-roster-title')).toContain('Ana Lim');
    expect(page.button('Remove Ana Lim from 3A')).toBeTruthy();
    expect(page.panel('school-students-title')).toContain('Enrolled');
    page.button('Enroll Bo Koh in 3A').click();
    await page.settle();
    expect(page.api.enroll).toHaveBeenCalledWith('c1', 's2');
    expect(page.root.textContent).toContain('Bo Koh enrolled in 3A.');
  });

  it('keeps another classroom’s roster when an enroll finishes after it was opened', async () => {
    const other: SchoolClassroom = { ...room, id: 'c2', name: '3B', students: 0 };
    let enrolled: () => void = () => undefined;
    const page = render([lee], [room, other], {
      enroll: vi.fn(
        () => new Promise((resolve) => (enrolled = () => resolve({ student_id: 's2', classroom_id: 'c1' }))),
      ),
      rosterPage: vi.fn((id: string) =>
        Promise.resolve(
          id === 'c1'
            ? { students: [{ id: 's1', username: 'ana', display_name: 'Ana Lim' }], total: 1 }
            : { students: [], total: 0 },
        ),
      ),
      rosterIds: vi.fn((id: string) => Promise.resolve(id === 'c1' ? ['s1'] : [])),
    });
    await page.settle();
    page.button('Manage students of 3A').click();
    await page.settle();
    page.button('Enroll Bo Koh in 3A').click();
    page.button('Manage students of 3B').click();
    await page.settle();
    enrolled();
    await page.settle();
    await page.settle();
    expect(page.panel('admin-roster-title')).toContain('Students in 3B');
    expect(page.panel('admin-roster-title')).not.toContain('Ana Lim');
  });

  it('asks before removing a student', async () => {
    const page = render([lee], [room]);
    await page.settle();
    page.button('Manage students of 3A').click();
    await page.settle();
    page.button('Remove Ana Lim from 3A').click();
    page.fixture.detectChanges();
    expect(page.root.querySelector('dialog[open]')?.textContent).toContain('Ana Lim will lose access to 3A');
    page.button('Remove student').click();
    await page.settle();
    expect(page.api.unenroll).toHaveBeenCalledWith('c1', 's1');
  });
});
