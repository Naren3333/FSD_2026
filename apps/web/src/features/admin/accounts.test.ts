// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AdminApi } from '../../api/clients';
import type { Account } from '../../api/models';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { AdminAccountsComponent, USERNAME } from './accounts';

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
const ana: Account = { id: 's1', username: 'ana', display_name: 'Ana Lim', role: 'student' };

function render(admin: Record<string, unknown> = {}) {
  TestBed.resetTestingModule();
  const api = {
    accounts: vi.fn((role: string) =>
      Promise.resolve(role === 'teacher' ? { accounts: [lee], total: 1 } : { accounts: [ana], total: 1 }),
    ),
    createAccount: vi.fn(() =>
      Promise.resolve({
        id: 's9',
        username: 'new.kid',
        display_name: 'New Kid',
        role: 'student',
        temporary_password: 'once-only-secret',
      }),
    ),
    resetPassword: vi.fn(() =>
      Promise.resolve({
        id: 't1',
        username: 'lee',
        display_name: 'Lee Tan',
        role: 'teacher',
        temporary_password: 'reset-secret',
      }),
    ),
    ...admin,
  };
  TestBed.configureTestingModule({ providers: [{ provide: AdminApi, useValue: api }] });
  TestBed.overrideComponent(AdminAccountsComponent, { set: { template: template('./accounts.html') } });
  for (const [component, path] of [
    [PageHeaderComponent, '../../shared/page-header.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [PagerComponent, '../../shared/pager.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(AdminAccountsComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  const fill = (id: string, value: string) => {
    const field = root.querySelector(`#${id}`) as HTMLInputElement | HTMLSelectElement;
    field.value = value;
    field.dispatchEvent(new Event(field instanceof HTMLSelectElement ? 'change' : 'input'));
    fixture.detectChanges();
  };
  const panel = (id: string) => root.querySelector(`[aria-labelledby="${id}"]`)?.textContent ?? '';
  return { root, fixture, settle, button, fill, panel, api, component: fixture.componentInstance };
}

describe('the username rule', () => {
  it('matches what identity accepts', () => {
    for (const ok of ['abc', 'Ana.Lim', 'a_b-c.9', 'x'.repeat(64)]) expect(USERNAME.test(ok)).toBe(true);
    for (const bad of ['ab', '.abc', 'a b c', 'ana@school', 'x'.repeat(65)])
      expect(USERNAME.test(bad)).toBe(false);
  });
});

describe('creating an account', () => {
  it('keeps the input and says what to fix when it is invalid', async () => {
    const page = render();
    await page.settle();
    page.fill('account-username', 'a b');
    page.button('Create account').click();
    await page.settle();
    expect(page.api.createAccount).not.toHaveBeenCalled();
    expect(page.root.textContent).toContain('Enter a first name of 1–60 characters.');
    expect(page.root.textContent).toContain('Use 3–64 letters, digits, dots, hyphens or underscores');
    expect((page.root.querySelector('#account-username') as HTMLInputElement).value).toBe('a b');
  });

  it('shows the temporary password once, until the admin is done with it', async () => {
    const page = render();
    await page.settle();
    page.fill('account-role', 'student');
    page.fill('account-first', ' New ');
    page.fill('account-last', 'Kid');
    page.fill('account-username', 'New.Kid');
    page.button('Create account').click();
    await page.settle();
    expect(page.api.createAccount).toHaveBeenCalledWith({
      role: 'student',
      username: 'New.Kid',
      first_name: 'New',
      last_name: 'Kid',
    });
    expect(page.panel('created-title')).toContain('New Kid can now sign in');
    expect(page.panel('created-title')).toContain('once-only-secret');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(true);
    page.button('Done').click();
    page.fixture.detectChanges();
    expect(page.root.textContent).not.toContain('once-only-secret');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(false);
  });

  it('copies the password, and says so when the browser refuses', async () => {
    const page = render();
    await page.settle();
    page.component.created.set({
      id: 's9',
      username: 'new.kid',
      display_name: 'New Kid',
      role: 'student',
      temporary_password: 'once-only-secret',
    });
    const writeText = vi.fn(() => Promise.reject(new Error('denied')));
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    page.fixture.detectChanges();
    page.button('Copy password').click();
    await page.settle();
    expect(writeText).toHaveBeenCalledWith('once-only-secret');
    expect(page.root.textContent).toContain('Copying was refused by the browser.');
  });

  it('reports a created account even when the list cannot be read again', async () => {
    let reads = 0;
    const page = render({
      accounts: vi.fn(() =>
        ++reads <= 2
          ? Promise.resolve({ accounts: [ana], total: 1 })
          : Promise.reject(new Error('Identity is unavailable.')),
      ),
    });
    await page.settle();
    page.button('Students').click();
    await page.settle();
    page.fill('account-first', 'New');
    page.fill('account-last', 'Kid');
    page.fill('account-username', 'new.kid');
    page.button('Create account').click();
    await page.settle();
    expect(page.panel('account-form-title')).toContain('Account created.');
    expect(page.panel('account-form-title')).not.toContain('Identity is unavailable.');
    expect(page.panel('created-title')).toContain('once-only-secret');
    expect(page.panel('accounts-title')).toContain('Identity is unavailable.');
  });

  it('explains a taken username in place and keeps the input', async () => {
    const page = render({
      createAccount: () => Promise.reject(new Error('Another account already has that username.')),
    });
    await page.settle();
    page.fill('account-first', 'Ana');
    page.fill('account-last', 'Lim');
    page.fill('account-username', 'ana');
    page.button('Create account').click();
    await page.settle();
    expect(page.panel('account-form-title')).toContain('Another account already has that username.');
    expect((page.root.querySelector('#account-username') as HTMLInputElement).value).toBe('ana');
  });
});

describe('the school accounts list', () => {
  it('lists teachers first and switches to students', async () => {
    const page = render();
    await page.settle();
    expect(page.panel('accounts-title')).toContain('Lee Tan');
    page.button('Students').click();
    await page.settle();
    expect(page.api.accounts).toHaveBeenLastCalledWith('student', '', 0, 20);
    expect(page.panel('accounts-title')).toContain('Ana Lim');
    expect(page.root.querySelector('[aria-pressed="true"]')?.textContent?.trim()).toBe('Students');
  });

  it('sends no search typed just before the page closes', async () => {
    const page = render();
    await page.settle();
    const reads = page.api.accounts.mock.calls.length;
    page.component.searchFor('ana');
    page.fixture.destroy();
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(page.api.accounts).toHaveBeenCalledTimes(reads);
  });

  it('explains an empty list', async () => {
    const page = render({ accounts: () => Promise.resolve({ accounts: [], total: 0 }) });
    await page.settle();
    expect(page.panel('accounts-title')).toContain('No teachers yet. Create their accounts above.');
  });
});

describe('resetting a password', () => {
  it('asks first, then shows the new temporary password once', async () => {
    const page = render();
    await page.settle();
    page.button('Reset password').click();
    page.fixture.detectChanges();
    expect(page.api.resetPassword).not.toHaveBeenCalled();
    const dialog = page.root.querySelector('dialog[open]') as HTMLDialogElement;
    expect(dialog.textContent).toContain("Lee Tan's current password stops working at once.");
    const confirm = [...dialog.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === 'Reset password',
    );
    confirm!.click();
    await page.settle();
    expect(page.api.resetPassword).toHaveBeenCalledWith('t1');
    expect(page.root.querySelector('dialog[open]')).toBeNull();
    expect(page.panel('created-title')).toContain('New temporary password for Lee Tan');
    expect(page.panel('created-title')).toContain('reset-secret');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(true);
  });

  it('keeps the dialog open with the reason when the reset fails', async () => {
    const page = render({ resetPassword: () => Promise.reject(new Error('Keycloak is unavailable.')) });
    await page.settle();
    page.button('Reset password').click();
    page.fixture.detectChanges();
    const dialog = page.root.querySelector('dialog[open]') as HTMLDialogElement;
    [...dialog.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Reset password')!.click();
    await page.settle();
    expect(page.root.querySelector('dialog[open]')?.textContent).toContain('Keycloak is unavailable.');
    expect(page.component.created()).toBeNull();
  });
});
