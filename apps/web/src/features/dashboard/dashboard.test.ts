// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AnalyticsApi, AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { AssessmentProgress, ClassPerformance, GradingCounts, Student } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { DashboardComponent } from './dashboard';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

interface Parts {
  roster?: Promise<Student[]>;
  progress?: Promise<AssessmentProgress[]>;
  counts?: Promise<GradingCounts>;
  performance?: Promise<ClassPerformance>;
  /** What the curriculum's names read answers; by default it has every topic. */
  need?: () => Promise<void>;
}

/** The user's classrooms: known by default, else still loading or failed with `error`. */
interface Rooms {
  known?: boolean;
  error?: string;
}

const students: Student[] = [
  { id: 's1', username: 'ana', display_name: 'Ana Lim' },
  { id: 's2', username: 'ben', display_name: 'Ben Tan' },
];
const published: AssessmentProgress = {
  id: 'a1',
  title: 'Fractions quiz',
  submissions: 1,
  status: 'published',
  due_at: null,
};
const performance: ClassPerformance = {
  classroom_id: 'c1',
  total: 2,
  offset: 0,
  interpretation: '',
  students: [
    {
      student_id: 's1',
      evidence_status: 'available',
      topics: [
        {
          topic_id: 'k1',
          attempted: 4,
          correct: 3,
          observed_accuracy: 0.75,
          evidence_status: 'insufficient_evidence',
          weakness_indicator: false,
        },
      ],
    },
    { student_id: 's2', evidence_status: 'no_finalized_evidence', topics: [] },
  ],
};

/** The class dashboard for classroom `classroomId`, each part answering as `parts` says (by default, a class with
 * data in every part). */
async function render(parts: Parts = {}, classroomId = 'c1', rooms: Rooms = {}) {
  const classPerformance = vi.fn(
    (_classroomId: string, _offset: number, _limit: number) =>
      parts.performance ?? Promise.resolve(performance),
  );
  const refresh = vi.fn(() => Promise.resolve());
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: IdentityApi, useValue: { roster: () => parts.roster ?? Promise.resolve(students) } },
      {
        provide: AssessmentApi,
        useValue: { progress: () => parts.progress ?? Promise.resolve([published]) },
      },
      {
        provide: GradingApi,
        useValue: {
          counts: () => parts.counts ?? Promise.resolve({ drafts: 3, finalized: 5, unreleased: 0 }),
        },
      },
      {
        provide: AnalyticsApi,
        useValue: { classPerformance },
      },
      {
        provide: CurriculumNames,
        useValue: {
          need: parts.need ?? (() => Promise.resolve()),
          topicName: (id: string) => (id === 'k1' ? 'Adding fractions' : ''),
        },
      },
      {
        provide: ClassroomContext,
        useValue: {
          classroomId: signal(classroomId),
          classroomName: signal(classroomId ? '3A' : ''),
          known: signal(rooms.known ?? true),
          loading: signal(false),
          error: signal(rooms.error ?? ''),
          refresh,
        },
      },
    ],
  });
  TestBed.overrideComponent(DashboardComponent, { set: { template: template('./dashboard.html') } });
  TestBed.overrideComponent(SectionStateComponent, {
    set: { template: template('../../shared/section-state.html') },
  });
  TestBed.overrideComponent(PagerComponent, { set: { template: template('../../shared/pager.html') } });
  const fixture = TestBed.createComponent(DashboardComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const alerts = () => [...root.querySelectorAll('[role="alert"]')].map((a) => a.textContent?.trim());
  /** A summary tile's value and caption, such as "3" and "5 finalized · Open Grading review". */
  const stat = (label: string) => {
    const tile = [...root.querySelectorAll('.stat')].find(
      (t) => t.querySelector('.stat-label')?.textContent === label,
    );
    return {
      value: tile?.querySelector('.stat-value')?.textContent?.trim(),
      caption: tile?.querySelector('.caption')?.textContent?.replace(/\s+/g, ' ').trim(),
    };
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  return { text, settle, alerts, stat, root, classPerformance, refresh, button, fixture };
}

describe('class dashboard states', () => {
  it('explains how to start when no classroom is chosen', async () => {
    const page = await render({}, '');
    expect(page.text()).toContain('No classroom selected.');
    expect(page.text()).not.toContain('Needs grading');
  });

  it('says the classrooms are loading, not that none is selected, until they are known', async () => {
    const page = await render({}, '', { known: false });
    expect(page.text()).toContain('Loading your classrooms…');
    expect(page.text()).not.toContain('No classroom selected.');
  });

  it('explains a failed classroom list with Try again, not that none is selected', async () => {
    const page = await render({}, '', { known: false, error: 'Identity is unavailable.' });
    expect(page.alerts()).toEqual(['Your classrooms could not be loaded. Identity is unavailable.']);
    expect(page.text()).not.toContain('No classroom selected.');
    page.button('Try again').click();
    expect(page.refresh).toHaveBeenCalledOnce();
  });

  it('shows each part loading until its data arrives', async () => {
    const never = new Promise<never>(() => undefined);
    const page = await render({ roster: never, progress: never, counts: never, performance: never });
    await page.settle();
    expect(page.text()).toContain('Loading assessments…');
    expect(page.text()).toContain('Loading class topics…');
    for (const label of ['Needs grading', 'Students enrolled', 'Published assessments'])
      expect(page.stat(label)).toEqual({ value: '…', caption: 'Loading…' });
    expect(page.text()).not.toContain('No published assessments yet');
  });

  it('renders every part from real data', async () => {
    const page = await render();
    await page.settle();
    expect(page.stat('Needs grading')).toEqual({ value: '3', caption: '5 finalized · Open Grading review' });
    expect(page.stat('Students enrolled').value).toBe('2');
    expect(page.text()).toContain('Fractions quiz');
    expect(page.text()).toContain('1 of 2 students submitted');
    expect(page.text()).toContain('Adding fractions');
    expect(page.text()).toContain('3 of 4 correct');
    expect(page.text()).toContain('Ana Lim');
    expect(page.alerts()).toEqual([]);
  });

  it('shows one failing part’s error in its place while the other parts still render', async () => {
    const page = await render({ counts: Promise.reject(new Error('Grading is unavailable.')) });
    await page.settle();
    expect(page.alerts()).toEqual(['Grading is unavailable.']);
    expect(page.stat('Needs grading')).toEqual({ value: '–', caption: 'Unavailable' });
    expect(page.stat('Students enrolled').value).toBe('2');
    expect(page.text()).toContain('Fractions quiz');
    expect(page.text()).toContain('Adding fractions');
  });

  it('keeps the grading count and roster when class topics fail', async () => {
    const page = await render({ performance: Promise.reject(new Error('Analytics is unavailable.')) });
    await page.settle();
    expect(page.alerts()).toEqual(['Analytics is unavailable.']);
    expect(page.stat('Needs grading').value).toBe('3');
    expect(page.stat('Students enrolled').value).toBe('2');
    expect(page.text()).toContain('Fractions quiz');
  });

  it('explains each empty part and the next step', async () => {
    const page = await render({
      roster: Promise.resolve([]),
      progress: Promise.resolve([{ ...published, status: 'draft' }]),
      counts: Promise.resolve({ drafts: 0, finalized: 0, unreleased: 0 }),
      performance: Promise.resolve({ ...performance, total: 0, students: [] }),
    });
    await page.settle();
    expect(page.stat('Needs grading').value).toBe('0');
    expect(page.stat('Students enrolled').value).toBe('0');
    // A draft has no submissions to count, so it is left out.
    expect(page.stat('Published assessments').value).toBe('0');
    expect(page.text()).toContain('No published assessments yet.');
    expect(page.text()).toContain('No students enrolled yet.');
  });

  it('says no grades are finalized when enrolled students have no evidence yet', async () => {
    const page = await render({
      performance: Promise.resolve({
        ...performance,
        students: performance.students.map((s) => ({ ...s, topics: [] })),
      }),
    });
    await page.settle();
    expect(page.text()).toContain('No finalized grades for these students yet.');
  });

  it('pages the class topics twenty students at a time', async () => {
    const page = await render({ performance: Promise.resolve({ ...performance, total: 45 }) });
    await page.settle();
    expect(page.text()).toContain('Page 1 of 3 · 45 students');
    const next = [...page.root.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Next');
    next?.click();
    await page.settle();
    expect(page.classPerformance).toHaveBeenLastCalledWith('c1', 20, 20);
    expect(page.text()).toContain('Page 2 of 3 · 45 students');
  });

  it('keeps the pager, and the Next button the teacher chose, disabled while the next page loads', async () => {
    const page = await render({ performance: Promise.resolve({ ...performance, total: 45 }) });
    await page.settle();
    page.classPerformance.mockReturnValueOnce(new Promise<never>(() => undefined));
    const next = page.button('Next');
    next.click();
    await page.settle();
    expect(page.text()).toContain('Loading class topics…');
    expect(page.button('Next')).toBe(next);
    expect(next.disabled).toBe(true);
    expect(page.button('Previous').disabled).toBe(true);
  });

  it('falls back to the last page when students left while the teacher was on a later one', async () => {
    const page = await render({ performance: Promise.resolve({ ...performance, total: 45 }) });
    await page.settle();
    page.button('Next').click();
    await page.settle();
    page.button('Next').click();
    await page.settle();
    expect(page.text()).toContain('Page 3 of 3 · 45 students');
    // Five students left: page 3 is now past the end.
    page.classPerformance.mockImplementation((_classroomId, offset) =>
      Promise.resolve({
        ...performance,
        total: 40,
        offset,
        students: offset >= 40 ? [] : performance.students,
      }),
    );
    page.button('Refresh dashboard').click();
    await page.settle();
    await page.settle();
    expect(page.classPerformance).toHaveBeenLastCalledWith('c1', 20, 20);
    expect(page.text()).toContain('Page 2 of 2 · 40 students');
  });

  it('shows topic ids when the curriculum cannot be read, and still the rest of the class topics', async () => {
    const page = await render({ need: () => Promise.reject(new Error('Curriculum is unavailable.')) });
    await page.settle();
    expect(page.alerts()).toEqual([]);
    expect(page.text()).toContain('3 of 4 correct');
  });
});
