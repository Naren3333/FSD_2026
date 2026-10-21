// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { CanActivateFn, CanMatchFn, Route, UrlTree, provideRouter } from '@angular/router';
import { beforeAll, describe, expect, it } from 'vitest';
import { IdentityApi } from './api/clients';
import { SECTIONS, routes } from './app.routes';
import { Auth } from './core/auth';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/* runs a route guard the way the router does, for a user with this role (or signed out). */
async function guard(
  path: string,
  role: 'teacher' | 'student' | null,
  profileFails = false,
): Promise<string | boolean> {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: Auth, useValue: { authenticated: role !== null } },
      {
        provide: IdentityApi,
        useValue: {
          register: () =>
            profileFails
              ? Promise.reject(new Error('Identity is unavailable.'))
              : Promise.resolve({ id: 'u1', org_id: 'o1', role, username: '', display_name: '' }),
        },
      },
    ],
  });
  const route = routes.find((r) => r.path === path) as Route;
  // a section open to both roles has no guard: the router opens it.
  if (!route.canActivate?.length && !route.canMatch?.length) return true;
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

  it('opens nothing while a signed-in user’s profile failed to load, so the shell can offer Try again', async () => {
    expect(await guard('classrooms', 'teacher', true)).toBe(false);
    expect(await guard('home', 'student', true)).toBe(false);
    expect(await guard('', 'student', true)).toBe(false);
  });
});

describe('sections for one role', () => {
  const teacherOnly = SECTIONS.filter((s) => s.teacherOnly).map((s) => s.path);
  const studentOnly = SECTIONS.filter((s) => s.studentOnly).map((s) => s.path);
  const shared = SECTIONS.filter((s) => !s.teacherOnly && !s.studentOnly).map((s) => s.path);

  it('lists the sections each role may not open', () => {
    expect(teacherOnly).toEqual(['dashboard', 'classrooms', 'curriculum', 'review']);
    expect(studentOnly).toEqual(['home', 'results']);
  });

  it.each(teacherOnly)(
    'sends a student from the teacher-only %s to Home, and opens it for a teacher',
    async (path) => {
      expect(await guard(path, 'student')).toBe('/home');
      expect(await guard(path, 'teacher')).toBe(true);
    },
  );

  it.each(studentOnly)(
    'sends a teacher from the student-only %s to the dashboard, and opens it for a student',
    async (path) => {
      expect(await guard(path, 'teacher')).toBe('/dashboard');
      expect(await guard(path, 'student')).toBe(true);
    },
  );

  it.each(shared)('opens the shared %s for both roles', async (path) => {
    expect(await guard(path, 'teacher')).toBe(true);
    expect(await guard(path, 'student')).toBe(true);
  });
});
