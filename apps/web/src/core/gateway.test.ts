// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { Auth } from './auth';
import { ApiError, Gateway } from './gateway';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const expired = signal(false);

beforeEach(() => {
  expired.set(false);
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [{ provide: Auth, useValue: { token: () => Promise.resolve('t'), expired } }],
  });
});

afterEach(() => vi.unstubAllGlobals());

/* the apierror a request fails with when the api answers status and body. */
async function failure(status: number, body: string): Promise<ApiError> {
  vi.stubGlobal('fetch', () => Promise.resolve(new Response(body, { status })));
  const error = await TestBed.inject(Gateway)
    .request('assessment', '/assessments/a1')
    .catch((e: unknown) => e);
  expect(error).toBeInstanceOf(ApiError);
  return error as ApiError;
}

describe('Gateway failures', () => {
  it("keep the API's message, status and code", async () => {
    const body = { error: { code: 'ASSESSMENT_CHANGED', message: 'Your teacher changed this assessment.' } };
    const error = await failure(409, JSON.stringify(body));
    expect([error.message, error.status, error.code]).toEqual([
      'Your teacher changed this assessment.',
      409,
      'ASSESSMENT_CHANGED',
    ]);
  });

  it('explain the status when the API says nothing, with no code', async () => {
    expect(await failure(403, '')).toMatchObject({
      message: 'You do not have access to this resource.',
      code: '',
    });
    expect((await failure(413, 'not json')).message).toContain('too large');
    expect((await failure(504, '{}')).message).toContain('took too long');
    expect((await failure(500, '{}')).message).toBe('The operation failed (500). Retry later.');
    expect(expired()).toBe(false);
  });

  it('mark the session expired on a 401', async () => {
    expect((await failure(401, '')).message).toBe('Your session expired. Sign in again.');
    expect(expired()).toBe(true);
  });
});
