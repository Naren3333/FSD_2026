// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { IdentityApi } from '../../api/clients';
import type { Classroom, Student } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { ClassroomsComponent } from './classrooms';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const ana: Student = { id: 's1', username: 'ana', display_name: 'Ana Lim' };

/** A teacher's Classrooms page, with these classrooms (the first chosen), whose roster and school list load as
 * given. */
function render(
  rooms: Classroom[],
  roster: Promise<Student[]>,
  everyone: Promise<{ students: Student[]; total: number }>,
  identity: Record<string, unknown> = {},
  known = true,
) {
  TestBed.resetTestingModule();
  const rosterPage = vi.fn(() => roster.then((list) => ({ students: list, total: list.length })));
  TestBed.configureTestingModule({
    providers: [
      { provide: Session, useValue: { isTeacher: signal(true), isStudent: signal(false) } },
      {
        provide: IdentityApi,
        useValue: {
          rosterPage,
          rosterIds: () => roster.then((list) => list.map((s) => s.id)),
          students: () => everyone,
          ...identity,
        },
      },
      {
        provide: ClassroomContext,
        useValue: {
          classrooms: signal(rooms),
          classroomId: signal(rooms[0]?.id ?? ''),
          classroomName: signal(rooms[0]?.name ?? ''),
          known: signal(known),
          loading: signal(!known),
          error: signal(''),
          refresh: () => Promise.resolve(),
        },
      },
    ],
  });
  TestBed.overrideComponent(ClassroomsComponent, { set: { template: template('./classrooms.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [PagerComponent, '../../shared/pager.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ClassroomsComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  /** The text of the panel with this heading id. */
  const panel = (id: string) => root.querySelector(`[aria-labelledby="${id}"]`)?.textContent ?? '';
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  return { root, panel, settle, button, component: fixture.componentInstance };
}

const room: Classroom = { id: 'c1', name: '3A', teacher_id: 't1' };
const never = new Promise<never>(() => undefined);

describe('classrooms states', () => {
  it('shows each list loading, not its empty text, until it arrives', async () => {
    const page = render([room], never, never);
    await page.settle();
    expect(page.panel('roster-title')).toContain('Loading students…');
    expect(page.panel('roster-title')).not.toContain('No students enrolled yet');
    expect(page.panel('everyone-title')).toContain('Loading students…');
    expect(page.panel('everyone-title')).not.toContain('No students have signed in yet');
  });

  it('explains empty lists and the next step', async () => {
    const page = render([], Promise.resolve([]), Promise.resolve({ students: [], total: 0 }));
    await page.settle();
    expect(page.root.textContent).toContain('No classrooms yet. Create one to get started.');
    expect(page.panel('roster-title')).toContain('Select a classroom to see its students.');
    expect(page.panel('everyone-title')).toContain('No students have signed in yet.');
  });

  it('says the classrooms are loading, not that there are none, until they are known', async () => {
    const page = render([], Promise.resolve([]), Promise.resolve({ students: [], total: 0 }), {}, false);
    await page.settle();
    expect(page.root.textContent).toContain('Loading your classrooms…');
    expect(page.root.textContent).not.toContain('No classrooms yet');
    expect(page.panel('roster-title')).not.toContain('Select a classroom');
  });

  it('says how to enroll students into an empty classroom', async () => {
    const page = render([room], Promise.resolve([]), Promise.resolve({ students: [ana], total: 1 }));
    await page.settle();
    expect(page.panel('roster-title')).toContain(
      'No students enrolled yet. Enroll them from All students below.',
    );
    expect(page.panel('everyone-title')).toContain('Ana Lim');
  });

  it('shows one list’s failure in place while the other still renders', async () => {
    const page = render(
      [room],
      Promise.reject(new Error('Identity is unavailable.')),
      Promise.resolve({ students: [ana], total: 1 }),
    );
    await page.settle();
    expect(page.panel('roster-title')).toContain('Identity is unavailable.');
    expect(page.panel('roster-title')).not.toContain('No students enrolled yet');
    expect(page.panel('everyone-title')).toContain('Ana Lim');
  });
});

describe('the classroom roster', () => {
  const students = (from: number, n: number): Student[] =>
    Array.from({ length: n }, (_, i) => ({ id: `s${from + i}`, username: `u${from + i}`, display_name: '' }));

  it('pages through every student, marking enrolled ones beyond the first page in the school list', async () => {
    const rosterPage = vi.fn((_c: string, offset: number) =>
      Promise.resolve({ students: students(offset, offset ? 5 : 20), total: 25 }),
    );
    const late: Student = { id: 's24', username: 'zed', display_name: 'Zed Ong' };
    const page = render([room], Promise.resolve([]), Promise.resolve({ students: [late], total: 1 }), {
      rosterPage,
      rosterIds: () => Promise.resolve(students(0, 25).map((s) => s.id)),
    });
    await page.settle();
    expect(page.panel('roster-title')).toContain('Page 1 of 2 · 25 students');
    expect(page.panel('everyone-title')).toContain('Enrolled');
    const next = page.button('Next');
    next.focus();
    next.click();
    expect(next.disabled).toBe(false);
    await page.settle();
    expect(rosterPage).toHaveBeenLastCalledWith('c1', 20, 20);
    expect(page.panel('roster-title')).toContain('Page 2 of 2 · 25 students');
  });

  it('falls back to the last page when students leave', async () => {
    let total = 21;
    const rosterPage = vi.fn((_c: string, offset: number) =>
      Promise.resolve({
        students: offset < total ? students(offset, Math.min(20, total - offset)) : [],
        total,
      }),
    );
    const page = render([room], Promise.resolve([]), Promise.resolve({ students: [], total: 0 }), {
      rosterPage,
    });
    await page.settle();
    page.button('Next').click();
    await page.settle();
    total = 20;
    page.button('Refresh students').click();
    await page.settle();
    expect(rosterPage).toHaveBeenLastCalledWith('c1', 0, 20);
    expect(page.component.rosterPage()).toBe(0);
  });
});
