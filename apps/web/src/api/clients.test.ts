// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { Gateway, type RequestOptions, type Service } from '../core/gateway';
import { AdminApi, AssessmentApi, ContentApi, CurriculumApi } from './clients';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/* what each client call asked the gateway for. */
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
  it('requests just the topic ids needed for names', async () => {
    await TestBed.inject(CurriculumApi).topicNames(['t1', 't2']);
    expect(calls).toEqual([
      {
        service: 'curriculum',
        path: '/topics/batch',
        options: { method: 'POST', body: { ids: ['t1', 't2'] } },
      },
    ]);
  });

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

describe('the school admin client', () => {
  it('reads every teacher, the pages after the first together, without repeats', async () => {
    const teacher = (n: number) => ({
      id: `t${n}`,
      username: `t${n}`,
      display_name: `T${n}`,
      role: 'teacher',
    });
    const pages = new Map([
      [0, Array.from({ length: 50 }, (_, i) => teacher(i))],
      [50, [...Array.from({ length: 49 }, (_, i) => teacher(i + 51)), teacher(49)]],
      [100, [teacher(100)]],
    ]);
    let pending = 0;
    let together = 0;
    TestBed.overrideProvider(Gateway, {
      useValue: {
        request: async (_service: Service, path: string) => {
          const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset'));
          pending++;
          together = Math.max(together, pending);
          await Promise.resolve();
          pending--;
          return { accounts: pages.get(offset) ?? [], total: 101 };
        },
      },
    });
    const teachers = await TestBed.inject(AdminApi).teachers();
    expect(teachers.map((t) => t.id)).toEqual([
      ...Array.from({ length: 50 }, (_, i) => `t${i}`),
      ...Array.from({ length: 49 }, (_, i) => `t${i + 51}`),
      't100',
    ]);
    expect(together).toBe(2);
  });

  it("reads a classroom's enrolled ids from the admin route in one request", async () => {
    const paths: string[] = [];
    TestBed.overrideProvider(Gateway, {
      useValue: {
        request: async (service: Service, path: string) => {
          paths.push(`${service} ${path}`);
          return ['s1', 's2'];
        },
      },
    });
    const room = '7f3c8a2e-4b1d-4c6e-9a5f-1e2d3c4b5a69';
    await expect(TestBed.inject(AdminApi).rosterIds(room)).resolves.toEqual(['s1', 's2']);
    expect(paths).toEqual([`identity /admin/classrooms/${room}/student-ids`]);
  });
});
