// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Gateway, type RequestOptions, type Service } from '../core/gateway';
import { AssessmentApi, ContentApi, CurriculumApi } from './clients';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/** What each client call asked the gateway for. */
let calls: { service: Service; path: string; options?: RequestOptions }[];

beforeEach(() => {
  calls = [];
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Gateway,
        useValue: {
          request: (service: Service, path: string, options?: RequestOptions) => {
            calls.push({ service, path, options });
            return Promise.resolve(path.includes('/page?') ? { items: [], total: 0 } : undefined);
          },
        },
      },
    ],
  });
});

describe('the clients for topics, documents and assessment changes', () => {
  it('lists archived topics apart and restores one', async () => {
    const curriculum = TestBed.inject(CurriculumApi);
    await curriculum.archivedTopics();
    await curriculum.restoreTopic('t/1');
    expect(calls).toEqual([
      { service: 'curriculum', path: '/topics/page?offset=0&limit=100&archived=true', options: undefined },
      { service: 'curriculum', path: '/topics/t%2F1/restore', options: { method: 'POST' } },
    ]);
  });

  it('deletes a document', async () => {
    await TestBed.inject(ContentApi).remove('d1');
    expect(calls).toEqual([{ service: 'content', path: '/documents/d1', options: { method: 'DELETE' } }]);
  });

  it('changes only the details with PATCH', async () => {
    const details = { title: 'Halves', due_at: null, late_policy: 'accept' as const };
    await TestBed.inject(AssessmentApi).details('a1', details);
    expect(calls).toEqual([
      { service: 'assessment', path: '/assessments/a1', options: { method: 'PATCH', body: details } },
    ]);
  });

  it('deletes with the submissions only when asked to', async () => {
    const assessments = TestBed.inject(AssessmentApi);
    await assessments.remove('a1');
    await assessments.remove('a1', true);
    expect(calls.map((c) => [c.path, c.options?.method])).toEqual([
      ['/assessments/a1', 'DELETE'],
      ['/assessments/a1?with_submissions=true', 'DELETE'],
    ]);
  });
});
