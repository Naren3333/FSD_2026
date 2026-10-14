// @vitest-environment jsdom
import '@angular/compiler';
import { computed, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { CanActivateFn, CanMatchFn, Route, UrlTree, provideRouter } from '@angular/router';
import { beforeAll, describe, expect, it } from 'vitest';
import { routes } from './app.routes';
import { Session } from './core/session';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/** Runs a route guard the way the router does, for a user with this role (or signed out). */
async function guard(path: string, role: 'teacher' | 'student' | null): Promise<string | boolean> {
  const profile = signal(role && { role });
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      {
        provide: Session,
        useValue: {
          profile,
          isStudent: computed(() => profile()?.role === 'student'),
          ready: () => Promise.resolve(),
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
    expect(await guard('', 'student')).toBe('/assessments');
    expect(await guard('', 'teacher')).toBe('/classrooms');
  });

  it('sends a role away from the other role’s sections', async () => {
    expect(await guard('classrooms', 'student')).toBe('/assessments');
    expect(await guard('results', 'teacher')).toBe('/classrooms');
    expect(await guard('classrooms', 'teacher')).toBe(true);
    expect(await guard('results', null)).toBe(true);
  });
});
