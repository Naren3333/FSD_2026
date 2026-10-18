// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation, EvaluationPage } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { ReviewComponent } from './review';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const quiz: Assessment = {
  id: 'a1',
  classroom_id: 'c1',
  title: 'Fractions quiz',
  questions: [
    { id: 'q1', prompt: 'Half of 1?', type: 'numerical', options: null, points: 1, topic_id: 'k1' },
  ],
};
const grade = (student: string, status: Evaluation['status']): Evaluation => ({
  id: `e-${student}`,
  assessment_id: 'a1',
  classroom_id: 'c1',
  student_id: student,
  status,
  evidence: [
    { question_id: 'q1', topic_id: 'k1', correct: true, awarded: 1, possible: 1, marked_by: 'auto' },
  ],
  answers: { q1: '0.5' },
  late: false,
  released_at: status === 'finalized' ? '2026-10-16T00:00:00Z' : null,
});

/** The Grading review page for classroom c1, whose grades load as `grades` says, all on one page; `grading`
 * replaces any of Grading's calls. */
function render(grades: Promise<Evaluation[]>, grading: Record<string, unknown> = {}) {
  TestBed.resetTestingModule();
  const evaluations = vi.fn(
    (): Promise<EvaluationPage> => grades.then((list) => ({ evaluations: list, total: list.length })),
  );
  TestBed.configureTestingModule({
    providers: [
      {
        provide: GradingApi,
        useValue: {
          evaluations,
          counts: () => Promise.resolve({ drafts: 0, finalized: 0, unreleased: 0 }),
          ...grading,
        },
      },
      {
        provide: IdentityApi,
        useValue: {
          roster: () =>
            Promise.resolve([
              { id: 's1', username: 'ana', display_name: 'Ana Lim' },
              { id: 's2', username: 'ben', display_name: 'Ben Tan' },
            ]),
        },
      },
      {
        provide: AssessmentApi,
        useValue: { list: () => Promise.resolve([]), get: () => Promise.resolve(quiz) },
      },
      { provide: CurriculumNames, useValue: { need: () => Promise.resolve(), topicName: () => 'Halves' } },
      {
        provide: ClassroomContext,
        useValue: {
          classroomId: signal('c1'),
          known: signal(true),
          loading: signal(false),
          error: signal(''),
          refresh: () => Promise.resolve(),
        },
      },
    ],
  });
  TestBed.overrideComponent(ReviewComponent, { set: { template: template('./review.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
    [PagerComponent, '../../shared/pager.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ReviewComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent ?? '';
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  /** The grade card of the student with this name. */
  const card = (name: string) =>
    [...root.querySelectorAll('article.grade')].find((a) =>
      a.querySelector('h3')?.textContent?.includes(name),
    );
  const buttons = (within: Element | undefined) =>
    [...(within?.querySelectorAll('button') ?? [])].map((b) => b.textContent?.trim());
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  return {
    fixture,
    root,
    component: fixture.componentInstance,
    text,
    settle,
    card,
    buttons,
    button,
    evaluations,
  };
}

describe('grading review states', () => {
  it('shows loading, not the empty text, until the grades arrive', async () => {
    const page = render(new Promise(() => undefined));
    await page.settle();
    expect(page.text()).toContain('Loading grades…');
    expect(page.text()).not.toContain('No grading evidence yet');
  });

  it('explains an empty classroom', async () => {
    const page = render(Promise.resolve([]));
    await page.settle();
    expect(page.text()).not.toContain('Loading grades…');
    expect(page.text()).toContain('No grading evidence yet.');
  });

  it('shows the failure, not the empty text, when the grades fail', async () => {
    const page = render(Promise.reject(new Error('Grading is unavailable.')));
    await page.settle();
    expect(page.text()).toContain('Grading is unavailable.');
    expect(page.text()).not.toContain('No grading evidence yet');
  });
});

describe('finalizing', () => {
  it('offers review and finalization only for drafts', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    await page.settle();
    expect(page.card('Ana Lim')?.textContent).toContain('Draft');
    expect(page.buttons(page.card('Ana Lim'))).toEqual(['Review grade']);
    expect(page.card('Ben Tan')?.textContent).toContain('Finalized');
    expect(page.buttons(page.card('Ben Tan'))).toEqual([]);
    expect(page.buttons(page.fixture.nativeElement as HTMLElement)).not.toContain('Finalize this grade');

    (page.card('Ana Lim')?.querySelector('button') as HTMLButtonElement).click();
    page.fixture.detectChanges();
    expect(page.buttons(page.card('Ana Lim'))).toContain('Finalize this grade');
    expect(page.buttons(page.card('Ben Tan'))).toEqual([]);
  });
});

describe('unsaved marks', () => {
  it('are not discarded by Refresh grades', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft')]));
    await page.settle();
    (page.card('Ana Lim')?.querySelector('button') as HTMLButtonElement).click();
    page.fixture.detectChanges();
    page.component.marks.markAsDirty();
    page.evaluations.mockClear();
    page.button('Refresh grades').click();
    await page.settle();
    expect(page.evaluations).not.toHaveBeenCalled();
    expect(page.text()).toContain('Save or close the grade you are marking first.');
    expect(page.buttons(page.card('Ana Lim'))).toContain('Finalize this grade');
  });
});

describe('pages of grades', () => {
  const many = (n: number) => Array.from({ length: n }, (_, i) => grade(`s${i}`, 'draft'));

  it('pages through every grade with the shared pager', async () => {
    const evaluations = vi.fn((_c: string, _a: string, offset: number) =>
      Promise.resolve({ evaluations: many(offset < 40 ? 20 : 5), total: 45 }),
    );
    const page = render(Promise.resolve([]), { evaluations });
    await page.settle();
    expect(page.text()).toContain('Page 1 of 3 · 45 grades');
    const next = page.button('Next');
    next.focus();
    next.click();
    page.fixture.detectChanges();
    // The button keeps focus while the page loads.
    expect(next.disabled).toBe(false);
    expect(document.activeElement).toBe(next);
    await page.settle();
    expect(evaluations).toHaveBeenLastCalledWith('c1', '', 20, 20);
    expect(page.text()).toContain('Page 2 of 3 · 45 grades');
  });

  it('falls back to the last page when the list shrinks', async () => {
    let total = 25;
    const evaluations = vi.fn((_c: string, _a: string, offset: number) =>
      Promise.resolve({ evaluations: offset < total ? many(Math.min(20, total - offset)) : [], total }),
    );
    const page = render(Promise.resolve([]), { evaluations });
    await page.settle();
    page.button('Next').click();
    await page.settle();
    total = 20;
    page.button('Refresh grades').click();
    await page.settle();
    expect(evaluations).toHaveBeenLastCalledWith('c1', '', 0, 20);
    expect(page.component.page()).toBe(0);
    expect(page.root.querySelectorAll('article.grade').length).toBe(20);
  });
});

describe('a change whose re-read fails', () => {
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

  it('reports a release, then that the grades could not be refreshed', async () => {
    let failing = false;
    const evaluations = vi.fn(() =>
      failing
        ? Promise.reject(new Error('Grading is unavailable.'))
        : Promise.resolve({ evaluations: [grade('s2', 'finalized')], total: 1 }),
    );
    const page = render(Promise.resolve([]), {
      evaluations,
      counts: () => Promise.resolve({ drafts: 0, finalized: 1, unreleased: 1 }),
      release: () => Promise.resolve({ assessment_id: 'a1', released: 1 }),
    });
    page.component.assessmentId.set('a1');
    page.component.load();
    await page.settle();
    expect(page.text()).toContain('1 finalized grade is hidden from students');
    failing = true;
    page.component.releasing.set(true);
    page.component.release();
    await page.settle();
    await page.settle();
    expect(page.component.releasing()).toBe(false);
    expect(page.text()).toContain('1 result released to students.');
    expect(page.text()).toContain('The grades could not be refreshed. Grading is unavailable.');
  });

  it('reports a finalized grade, then that the grades could not be refreshed', async () => {
    let failing = false;
    const evaluations = vi.fn(() =>
      failing
        ? Promise.reject(new Error('Grading is unavailable.'))
        : Promise.resolve({ evaluations: [grade('s1', 'draft')], total: 1 }),
    );
    const page = render(Promise.resolve([]), {
      evaluations,
      finalize: () => Promise.resolve(grade('s1', 'finalized')),
    });
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.component.form.controls.reason.setValue('Checked');
    failing = true;
    page.component.finalize();
    await page.settle();
    await page.settle();
    expect(page.text()).toContain('Grade finalized.');
    expect(page.text()).toContain('The grades could not be refreshed. Grading is unavailable.');
    expect(page.card('Ana Lim')).toBeDefined();
  });
});
