// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AppComponent } from './app';
import { AiUsage } from './core/ai-usage';
import { Auth } from './core/auth';
import { StatusComponent } from './shared/status';
import { UsageComponent } from './features/usage/usage';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
afterEach(() => vi.unstubAllGlobals());

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
      provideRouter([]),
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
  return { text, button, settle, auth, calls };
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
    expect(page.text()).toContain('Teacher · o1');
  });
});
