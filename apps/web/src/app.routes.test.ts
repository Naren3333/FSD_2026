// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { CanActivateFn, CanMatchFn, Route, UrlTree, provideRouter } from '@angular/router';
import { beforeAll, describe, expect, it } from 'vitest';
import { IdentityApi } from './api/clients';
import { routes } from './app.routes';
import { Auth } from './core/auth';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/** Runs a route guard the way the router does, for a user with this role (or signed out). The real Session reads
 * the role, from a fake sign-in and a fake Identity. */
async function guard(path: string, role: 'teacher' | 'student' | null): Promise<string | boolean> {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: Auth, useValue: { authenticated: role !== null } },
      {
        provide: IdentityApi,
        useValue: {
          register: () => Promise.resolve({ id: 'u1', org_id: 'o1', role, username: '', display_name: '' }),
        },
      },
    ],
  });
  const route = routes.find((r) => r.path === path) as Route;
  const result = await TestBed.runInInjectionContext(() =>
    route.canActivate
      ? (route.canActivate[0] as CanActivateFn)({} as never, {} as never)
      : (route.canMatch![0] as CanMatchFn)(route, []),
  );
  return result instanceof UrlTree ? result.toString() : (result as boolean);
}

describe('role landing pages', () => {
  it('sends each role from the root to its own landing page', async () => {
    expect(await guard('', 'student')).toBe('/home');
    expect(await guard('', 'teacher')).toBe('/dashboard');
  });

  it('sends a role away from the other role’s sections', async () => {
    expect(await guard('classrooms', 'student')).toBe('/home');
    expect(await guard('dashboard', 'student')).toBe('/home');
    expect(await guard('home', 'teacher')).toBe('/dashboard');
    expect(await guard('results', 'teacher')).toBe('/dashboard');
    expect(await guard('classrooms', 'teacher')).toBe(true);
    expect(await guard('dashboard', 'teacher')).toBe(true);
    expect(await guard('home', 'student')).toBe(true);
    expect(await guard('results', null)).toBe(true);
  });
});
