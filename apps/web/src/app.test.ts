// @vitest-environment jsdom
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { Router, provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AppComponent } from './app';
import { AiUsage } from './core/ai-usage';
import { Auth } from './core/auth';
import { UnsavedChanges } from './core/unsaved-changes';
import { StatusComponent } from './shared/status';
import { UsageComponent } from './features/usage/usage';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
afterEach(() => vi.unstubAllGlobals());

@Component({ standalone: true, template: '' })
class StubPage {}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

/* the shell for a user signed in with keycloak, whose api calls `fetch` answers; identity, gateway and session are the real ones. */
function render(fetch: (url: string) => Response) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      calls.push(url);
      return Promise.resolve(fetch(url));
    }),
  );
  const auth = {
    authenticated: true,
    expired: signal(false),
    token: () => Promise.resolve('token'),
    login: vi.fn(() => Promise.resolve()),
    logout: vi.fn(() => Promise.resolve()),
  };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: 'elsewhere', component: StubPage },
        { path: 'review', component: StubPage },
      ]),
      { provide: Auth, useValue: auth },
      {
        provide: AiUsage,
        useValue: {
          usage: signal(null),
          loading: signal(false),
          failed: signal(false),
          refresh: () => undefined,
        },
      },
    ],
  });
  TestBed.overrideComponent(AppComponent, { set: { template: template('./app.html') } });
  TestBed.overrideComponent(StatusComponent, { set: { template: template('./shared/status.html') } });
  TestBed.overrideComponent(UsageComponent, { set: { template: '' } });
  const fixture = TestBed.createComponent(AppComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent ?? '';
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  const settle = async () => {
    await vi.waitFor(() => expect(fixture.componentInstance.state.busy()).toBe(false));
    await fixture.whenStable();
    fixture.detectChanges();
  };
  return { root, fixture, text, button, settle, auth, calls };
}

describe('an account signing in with a role it was not registered with', () => {
  it.each([
    [
      403,
      'This sign-in carries both the teacher and student roles. Ask your administrator to fix your account.',
    ],
    [409, 'This account is registered as a student. Sign in with your student account.'],
  ])('shows the server’s ROLE_CONFLICT message (%i) with Try again and Sign out', async (status, message) => {
    const page = render(() => json(status, { error: { code: 'ROLE_CONFLICT', message } }));
    expect(page.text()).toContain('Loading your workspace…');
    await page.settle();
    expect(page.text()).toContain("Your workspace didn't load");
    expect(page.text()).toContain(message);
    expect(page.text()).not.toContain('Loading your workspace…');
    // not stuck: neither control waits on anything, and signing out works.
    expect(page.button('Try again').disabled).toBe(false);
    expect(page.button('Sign out').disabled).toBe(false);
    page.button('Sign out').click();
    expect(page.auth.logout).toHaveBeenCalledTimes(1);
  });

  it('asks Identity again on Try again, and opens the workspace once the account’s role is accepted', async () => {
    let conflict = true;
    const page = render((url) =>
      url.endsWith('/api/identity/me')
        ? conflict
          ? json(409, { error: { code: 'ROLE_CONFLICT', message: 'Sign in with your teacher account.' } })
          : json(200, { id: 'u1', org_id: 'o1', role: 'teacher', username: 't', display_name: 'T' })
        : json(200, []),
    );
    await page.settle();
    expect(page.text()).toContain('Sign in with your teacher account.');

    conflict = false;
    page.button('Try again').click();
    await page.settle();
    expect(page.calls.filter((url) => url.endsWith('/api/identity/me'))).toHaveLength(2);
    expect(page.text()).not.toContain('Sign in with your teacher account.');
    expect(page.text()).toContain('T');
    expect(page.text()).toContain('Teacher');
    expect(page.text()).not.toContain('o1');
  });
});

/* a teacher with one classroom c1, whose grading counts answer as given. */
async function teacher(counts: unknown = { drafts: 7, finalized: 0, unreleased: 0 }) {
  const page = render((url) =>
    url.endsWith('/api/identity/me')
      ? json(200, { id: 'u1', org_id: 'o1', role: 'teacher', username: 't', display_name: 'T' })
      : url.includes('/classrooms/page')
        ? json(200, { items: [{ id: 'c1', name: 'Sec 3 Science', teacher_id: 'u1' }], total: 1 })
        : url.includes('/evaluations/counts')
          ? json(200, counts)
          : json(200, []),
  );
  await page.settle();
  await vi.waitFor(() => {
    page.fixture.detectChanges();
    expect(page.root.querySelector('select')?.value).toBe('c1');
  });
  return page;
}

/* a control's accessible name from its content: hidden and aria-hidden parts left out. */
function nameOf(element: Element): string {
  const copy = element.cloneNode(true) as Element;
  copy.querySelectorAll('[aria-hidden="true"], [hidden]').forEach((part) => part.remove());
  return copy.textContent?.replace(/\s+/g, ' ').trim() ?? '';
}

describe('the signed-in shell', () => {
  it('shows the account block without the org id', async () => {
    const page = await teacher();
    const account = page.root.querySelector('.account')!;
    expect(account.textContent).toContain('T');
    expect(account.textContent).toContain('Teacher');
    expect(page.text()).not.toContain('o1');
    expect(page.root.querySelector('.identity-note')).toBeNull();
    expect(nameOf(page.button('Sign out'))).toBe('Sign out');
  });

  it('counts draft grades beside Grading review, keeping the link’s name', async () => {
    const page = await teacher();
    const link = () =>
      [...page.root.querySelectorAll('nav a')].find(
        (a) => nameOf(a) === 'Grading review',
      ) as HTMLAnchorElement;
    await vi.waitFor(() => {
      page.fixture.detectChanges();
      expect(link().querySelector('.nav-count')?.textContent?.trim()).toBe('7');
    });
    expect(link().querySelector('.nav-count')?.getAttribute('aria-hidden')).toBe('true');
    const described = page.root.querySelector('#' + link().getAttribute('aria-describedby'));
    expect(described?.textContent?.trim()).toBe('7 grades to review');
  });

  it('shows no count for an answer without one, and no error', async () => {
    const page = await teacher({});
    await page.fixture.whenStable();
    page.fixture.detectChanges();
    expect(page.root.querySelector('.nav-count')).toBeNull();
    expect(page.root.querySelector('[role="alert"]')).toBeNull();
  });

  it('locks the classroom list while work is unsaved, saying why', async () => {
    const page = await teacher();
    TestBed.runInInjectionContext(() => TestBed.inject(UnsavedChanges).track(() => true));
    // a form's input event marks the shell for checking in the app.
    page.fixture.componentRef.changeDetectorRef.markForCheck();
    page.fixture.detectChanges();
    const select = page.root.querySelector('select')!;
    expect(select.disabled).toBe(true);
    const reason = page.root.querySelector('#' + select.getAttribute('aria-describedby'));
    expect(reason?.textContent?.trim()).toBe('Finish or clear your current form before changing classrooms.');
  });

  it('opens the menu drawer, and closes it on Escape with focus back on Open menu', async () => {
    const page = await teacher();
    // focus needs the page in the document.
    document.body.append(page.root);
    const menu = page.root.querySelector('[aria-label="Open menu"]') as HTMLButtonElement;
    menu.click();
    page.fixture.detectChanges();
    expect(menu.getAttribute('aria-expanded')).toBe('true');
    await vi.waitFor(() => expect(document.activeElement?.classList.contains('brand')).toBe(true));
    expect(page.root.querySelector('main')?.hasAttribute('inert')).toBe(true);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    page.fixture.detectChanges();
    expect(menu.getAttribute('aria-expanded')).toBe('false');
    expect(page.root.querySelector('main')?.hasAttribute('inert')).toBe(false);
    await vi.waitFor(() => expect(document.activeElement).toBe(menu));
    page.root.remove();
  });

  it('closes the menu drawer on navigation', async () => {
    const page = await teacher();
    const menu = page.root.querySelector('[aria-label="Open menu"]') as HTMLButtonElement;
    menu.click();
    page.fixture.detectChanges();
    await TestBed.inject(Router).navigateByUrl('/elsewhere');
    page.fixture.detectChanges();
    expect(menu.getAttribute('aria-expanded')).toBe('false');
  });
});
