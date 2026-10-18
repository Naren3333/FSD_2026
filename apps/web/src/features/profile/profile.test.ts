// @vitest-environment jsdom
import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../core/gateway';
import { IdentityApi } from '../../api/clients';
import type { Classroom, Profile } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { StatusComponent } from '../../shared/status';
import { ProfileComponent } from './profile';

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

const student: Profile = {
  id: 'u1',
  org_id: 'school',
  role: 'student',
  username: 'ada',
  display_name: 'Ada L',
};

/** The Profile page for this user, whose deletion answers as `remove` does. */
function render(
  profile: Profile,
  rooms: Classroom[] = [],
  remove = () => Promise.resolve(),
  unsaved = false,
) {
  const logout = vi.fn(() => Promise.resolve());
  const deleteAccount = vi.fn(remove);
  const current = signal(profile);
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: {
          profile: current,
          isTeacher: computed(() => current().role === 'teacher'),
          isStudent: computed(() => current().role === 'student'),
          logout,
        },
      },
      { provide: IdentityApi, useValue: { deleteAccount } },
      { provide: ClassroomContext, useValue: { classrooms: signal(rooms) } },
      { provide: UnsavedChanges, useValue: { any: () => unsaved } },
    ],
  });
  TestBed.overrideComponent(ProfileComponent, { set: { template: template('./profile.html') } });
  for (const [component, path] of [
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ProfileComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { root, button, settle, logout, deleteAccount, fixture };
}

describe('the Profile page', () => {
  it('shows the account and deletes it only after the dialog, then signs out', async () => {
    const page = render(student);
    expect(page.root.textContent).toContain('Ada L');
    expect(page.root.textContent).toContain('ada');
    expect(page.root.textContent).toContain('your grades and progress');
    page.button('Delete account').click();
    page.fixture.detectChanges();
    const dialog = page.root.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(page.deleteAccount).not.toHaveBeenCalled();
    page.button('Delete my account').click();
    await page.settle();
    expect(page.deleteAccount).toHaveBeenCalledOnce();
    expect(page.logout).toHaveBeenCalledOnce();
  });

  it('keeps the dialog open and says why when the deletion fails, without signing out', async () => {
    const page = render(student, [], () => Promise.reject(new ApiError('Identity is unavailable.', 503)));
    page.button('Delete account').click();
    page.fixture.detectChanges();
    page.button('Delete my account').click();
    await page.settle();
    const dialog = page.root.querySelector('dialog') as HTMLDialogElement;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain('Identity is unavailable.');
    expect(page.logout).not.toHaveBeenCalled();
  });

  it('asks a teacher to delete their classrooms first', () => {
    const teacher: Profile = { ...student, role: 'teacher' };
    const page = render(teacher, [{ id: 'c1', name: '3A', teacher_id: 'u1' }]);
    expect(page.root.textContent).toContain('You still have 1 classroom.');
    expect(page.button('Delete account').disabled).toBe(true);
    expect(page.button('Delete account').getAttribute('aria-describedby')).toBe('delete-account-blocked');
    expect(render(teacher).button('Delete account').disabled).toBe(false);
  });

  it('opens no dialog while another page has unsaved work', () => {
    const page = render(student, [], undefined, true);
    page.button('Delete account').click();
    page.fixture.detectChanges();
    expect((page.root.querySelector('dialog') as HTMLDialogElement).open).toBe(false);
    expect(page.root.textContent).toContain('Finish or clear your unsaved forms');
  });
});
