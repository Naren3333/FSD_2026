// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { Router, provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { IdentityApi } from '../../api/clients';
import type { Classroom } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent } from '../../shared/pager';
import { StatusComponent } from '../../shared/status';
import { ClassroomsComponent } from './classrooms';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
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

/* a teacher's classrooms page, with these classrooms (the first chosen). */
function render(rooms: Classroom[], known = true) {
  TestBed.resetTestingModule();
  const classroomId = signal(rooms[0]?.id ?? '');
  const select = vi.fn((id: string) => classroomId.set(id));
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: 'classrooms', children: [] },
        { path: 'dashboard', children: [] },
        { path: 'students', children: [] },
      ]),
      { provide: Session, useValue: { isTeacher: signal(true), isStudent: signal(false) } },
      { provide: IdentityApi, useValue: {} },
      {
        provide: ClassroomContext,
        useValue: {
          classrooms: signal(rooms),
          classroomId,
          classroomName: signal(rooms[0]?.name ?? ''),
          known: signal(known),
          loading: signal(!known),
          error: signal(''),
          refresh: () => Promise.resolve(),
          select,
        },
      },
    ],
  });
  TestBed.overrideComponent(ClassroomsComponent, { set: { template: template('./classrooms.html') } });
  for (const [component, path] of [
    [PageHeaderComponent, '../../shared/page-header.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [PagerComponent, '../../shared/pager.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ClassroomsComponent);
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
  /* the card named by this classroom's heading. */
  const card = (name: string) =>
    [...root.querySelectorAll('article.classroom-card')].find(
      (c) => c.querySelector('h3')?.textContent?.trim() === name,
    ) as HTMLElement;
  return { root, fixture, settle, button, card, select, component: fixture.componentInstance };
}

const room: Classroom = { id: 'c1', name: '3A', teacher_id: 't1' };
const other: Classroom = { id: 'c2', name: '4B', teacher_id: 't1' };

describe('classrooms states', () => {
  it('says how to create the first classroom', async () => {
    const page = render([]);
    await page.settle();
    expect(page.root.textContent).toContain('No classrooms yet. Create your first one above.');
  });

  it('says the classrooms are loading, not that there are none, until they are known', async () => {
    const page = render([], false);
    await page.settle();
    expect(page.root.textContent).toContain('Loading your classrooms…');
    expect(page.root.textContent).not.toContain('No classrooms yet');
  });

  it('no longer lists students', async () => {
    const page = render([room]);
    await page.settle();
    expect(page.root.querySelector('#roster-title')).toBeNull();
    expect(page.root.querySelector('#everyone-title')).toBeNull();
  });
});

describe('classroom cards', () => {
  it('marks the current classroom and links it to its students', async () => {
    const page = render([room, other]);
    await page.settle();
    expect(page.card('3A').textContent).toContain('Current');
    expect(page.card('3A').querySelector('a')?.getAttribute('href')).toBe('/students');
    expect(page.card('4B').textContent).not.toContain('Current');
    expect(page.card('4B').querySelector('a')).toBeNull();
  });

  it('opens another classroom by choosing it and going to its dashboard', async () => {
    const page = render([room, other]);
    await page.settle();
    page.button('Open 4B').click();
    await page.settle();
    expect(page.select).toHaveBeenCalledWith('c2');
    expect(TestBed.inject(Router).url).toBe('/dashboard');
  });

  it('opens the current classroom’s dashboard without choosing it again', async () => {
    const page = render([room, other]);
    await page.settle();
    page.button('Open 3A').click();
    await page.settle();
    expect(page.select).not.toHaveBeenCalled();
    expect(TestBed.inject(Router).url).toBe('/dashboard');
  });

  it('keeps other classrooms closed while a form holds unsaved work', async () => {
    const page = render([room, other]);
    await page.settle();
    expect(page.root.querySelector('#classroom-open-lock')).toBeNull();
    page.component.form.markAsDirty();
    page.fixture.detectChanges();
    expect(TestBed.inject(UnsavedChanges).any()).toBe(true);
    const open = page.button('Open 4B');
    expect(open.disabled).toBe(true);
    expect(open.getAttribute('aria-describedby')).toBe('classroom-open-lock');
    expect(page.root.querySelector('#classroom-open-lock')?.textContent?.trim()).toBe(
      'Finish or clear your current form before changing classrooms.',
    );
    expect(page.button('Open 3A').disabled).toBe(false);
    page.component.open(other);
    expect(page.select).not.toHaveBeenCalled();
  });

  it('asks before deleting a classroom', async () => {
    const page = render([room, other]);
    await page.settle();
    page.button('Delete 4B').click();
    page.fixture.detectChanges();
    expect(page.component.deleting()).toEqual(other);
    expect(page.root.querySelector('dialog')?.textContent).toContain('Delete this classroom?');
    expect(page.root.querySelector('dialog')?.textContent).toContain('4B will disappear');
  });
});
