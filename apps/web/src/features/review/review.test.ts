// @vitest-environment jsdom
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { Router, provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, onTestFinished, vi } from 'vitest';
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
import { PageHeaderComponent } from '../../shared/page-header';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
// jsdom has no modal dialogs.
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
afterEach(() => vi.useRealTimers());

@Component({ standalone: true, template: '' })
class StubPage {}

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

/* the grading review page for classroom c1, whose grades load as `grades` says, all on one page; `grading` replaces any of grading's calls. */
function render(grades: Promise<Evaluation[]>, grading: Record<string, unknown> = {}) {
  TestBed.resetTestingModule();
  const evaluations = vi.fn(
    (): Promise<EvaluationPage> => grades.then((list) => ({ evaluations: list, total: list.length })),
  );
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: 'review', component: StubPage },
        { path: 'home', component: StubPage },
      ]),
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
  TestBed.overrideComponent(PageHeaderComponent, {
    set: { template: template('../../shared/page-header.html') },
  });
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
  /* the grade card of the student with this name. */
  const card = (name: string) =>
    [...root.querySelectorAll('article.grade')].find((a) =>
      a.querySelector('h3')?.textContent?.includes(name),
    );
  /* the grade panel beside the queue. */
  const panel = () => root.querySelector('section.grade-panel') ?? undefined;
  const buttons = (within: Element | null | undefined) =>
    [...(within?.querySelectorAll('button') ?? [])].map((b) => b.textContent?.trim());
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find(
      (b) => b.textContent?.trim() === name || b.getAttribute('aria-label') === name,
    ) as HTMLButtonElement;
  return {
    fixture,
    root,
    component: fixture.componentInstance,
    text,
    settle,
    card,
    panel,
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
    expect(page.buttons(page.card('Ben Tan'))).toEqual(['View grade']);
    expect(page.buttons(page.root)).not.toContain('Finalize this grade');

    (page.card('Ana Lim')?.querySelector('button') as HTMLButtonElement).click();
    page.fixture.detectChanges();
    expect(page.buttons(page.panel())).toContain('Finalize this grade');
    expect(page.buttons(page.card('Ana Lim'))).toEqual(['Review grade']);
    expect(page.card('Ana Lim')?.getAttribute('aria-current')).toBe('true');
  });

  it('shows a finalized grade read-only with View grade', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    await page.settle();
    (page.card('Ben Tan')?.querySelector('button') as HTMLButtonElement).click();
    page.fixture.detectChanges();
    const panel = page.panel();
    expect(page.component.viewing()?.id).toBe('e-s2');
    expect(page.component.selected()).toBeNull();
    expect(panel?.textContent).toContain('Ben Tan · Fractions quiz');
    expect(panel?.textContent).toContain('Half of 1?');
    expect(panel?.textContent?.replace(/\s+/g, ' ')).toContain('Topic: Halves');
    expect(panel?.querySelectorAll('input, textarea').length).toBe(0);
    expect(page.buttons(panel)).not.toContain('Finalize this grade');
  });

  it('shows an override left invalid, so finalizing can focus it', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft')]));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    page.button('Override').click();
    page.component.marks.controls['q1'].controls.awarded.setValue(5);
    page.button('Override').click();
    expect(page.component.overriding().has('q1')).toBe(false);
    page.component.askFinalize();
    expect(page.component.overriding().has('q1')).toBe(true);
    expect(page.component.finalizing()).toBe(false);
  });

  it('shows a release on the finalized grade open in the panel', async () => {
    let released = false;
    const finalized = () => ({
      ...grade('s2', 'finalized'),
      released_at: released ? '2026-10-20T00:00:00Z' : null,
    });
    const page = render(Promise.resolve([]), {
      evaluations: vi.fn(() => Promise.resolve({ evaluations: [finalized()], total: 1 })),
      counts: () => Promise.resolve({ drafts: 0, finalized: 1, unreleased: released ? 0 : 1 }),
      release: () => {
        released = true;
        return Promise.resolve({ assessment_id: 'a1', released: 1 });
      },
    });
    (TestBed.inject(AssessmentApi) as unknown as { list: () => Promise<unknown> }).list = () =>
      Promise.resolve([{ id: 'a1', title: 'Fractions quiz', status: 'published' }]);
    page.component.assessmentId.set('a1');
    page.component.load();
    await page.settle();
    page.component.view(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    expect(page.panel()?.textContent).toContain('Not released');
    page.component.release();
    await page.settle();
    await page.settle();
    expect(page.component.viewing()?.id).toBe('e-s2');
    expect(page.panel()?.textContent).not.toContain('Not released');
  });

  it('opens the next draft after finalizing, keeping the finalized message exactly', async () => {
    const list = [grade('s1', 'draft'), grade('s2', 'draft')];
    const page = render(Promise.resolve([]), {
      evaluations: vi.fn(() => Promise.resolve({ evaluations: [...list], total: list.length })),
      finalize: () => {
        list[0] = grade('s1', 'finalized');
        return Promise.resolve(list[0]);
      },
    });
    await page.settle();
    page.component.review(list[0]);
    page.component.form.controls.reason.setValue('Checked');
    page.component.finalize();
    await page.settle();
    await page.settle();
    expect(page.component.selected()?.id).toBe('e-s2');
    const status = page.root.querySelector('[role="status"]');
    expect(status?.textContent?.trim()).toBe(
      'Grade finalized. Performance will update after event processing.',
    );
  });
});

describe('the status filter', () => {
  it('hides rows and offers Show all when nothing matches', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    await page.settle();
    expect(page.button('Drafts (1)')).toBeDefined();
    page.button('Finalized (1)').click();
    page.fixture.detectChanges();
    expect(page.card('Ana Lim')).toBeUndefined();
    expect(page.card('Ben Tan')).toBeDefined();
    expect(page.button('Finalized (1)').getAttribute('aria-pressed')).toBe('true');
    page.button('To mark (0)').click();
    page.fixture.detectChanges();
    expect(page.text()).toContain('No grades to mark on this page.');
    page.button('Show all').click();
    page.fixture.detectChanges();
    expect(page.card('Ana Lim')).toBeDefined();
    expect(page.card('Ben Tan')).toBeDefined();
  });

  it('keeps the open grade and its unsaved marks', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    page.component.marks.markAsDirty();
    page.button('Finalized (1)').click();
    page.fixture.detectChanges();
    expect(page.card('Ana Lim')).toBeUndefined();
    expect(page.component.selected()?.id).toBe('e-s1');
    expect(page.buttons(page.panel())).toContain('Finalize this grade');
    expect(page.text()).not.toContain('Save or close the grade you are marking first.');
  });
});

describe('next to mark', () => {
  const list = () => [
    grade('s1', 'draft'),
    grade('s2', 'finalized'),
    grade('s3', 'draft'),
    grade('s4', 'finalized'),
  ];

  it('skips finalized grades and wraps to earlier drafts', async () => {
    const page = render(Promise.resolve(list()));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    page.button('Next to mark').click();
    expect(page.component.selected()?.id).toBe('e-s3');
    page.fixture.detectChanges();
    page.button('Next to mark').click();
    expect(page.component.selected()?.id).toBe('e-s1');
  });

  it('is disabled with nothing else to mark', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    expect(page.button('Next to mark').disabled).toBe(true);
    expect(page.panel()?.textContent).toContain('Nothing left to mark here');
  });

  it('continues on the next page and opens its first draft', async () => {
    const evaluations = vi.fn((_c: string, _a: string, offset: number) =>
      Promise.resolve({
        evaluations: offset
          ? [{ ...grade('s2', 'draft'), id: 'e-next' }]
          : [grade('s1', 'draft'), ...Array.from({ length: 19 }, (_, i) => grade(`f${i}`, 'finalized'))],
        total: 21,
      }),
    );
    const page = render(Promise.resolve([]), { evaluations });
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    expect(page.panel()?.textContent).toContain('Continues on the next page');
    page.button('Next to mark').click();
    await page.settle();
    expect(evaluations).toHaveBeenLastCalledWith('c1', '', 20, 20);
    expect(page.component.selected()?.id).toBe('e-next');
  });

  it('opens no draft on a later load when the next page failed', async () => {
    let failing = false;
    const evaluations = vi.fn((_c: string, _a: string, offset: number) =>
      failing
        ? Promise.reject(new Error('Grading is unavailable.'))
        : Promise.resolve({
            evaluations: offset ? [grade('s2', 'draft')] : [grade('s1', 'draft')],
            total: 21,
          }),
    );
    const page = render(Promise.resolve([]), { evaluations });
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    failing = true;
    page.button('Next to mark').click();
    await page.settle();
    expect(page.component.open()).toBeNull();
    failing = false;
    page.component.page.set(0);
    page.component.showPage();
    await page.settle();
    expect(page.component.open()).toBeNull();
  });

  it('refuses while marks are unsaved and keeps the grade', async () => {
    const page = render(Promise.resolve(list()));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    page.component.marks.markAsDirty();
    page.button('Next to mark').click();
    page.fixture.detectChanges();
    expect(page.component.selected()?.id).toBe('e-s1');
    expect(page.text()).toContain('Save or close the grade you are marking first.');
  });

  it('moves Previous grade along the queue as shown', async () => {
    const page = render(Promise.resolve(list()));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[2] as Evaluation);
    page.fixture.detectChanges();
    page.button('Previous grade').click();
    expect(page.component.viewing()?.id).toBe('e-s2');
    page.fixture.detectChanges();
    page.button('Previous grade').click();
    page.fixture.detectChanges();
    expect(page.component.selected()?.id).toBe('e-s1');
    expect(page.button('Previous grade').disabled).toBe(true);
  });

  it('keeps unsaved marks on All grades, while Close discards them', async () => {
    const page = render(Promise.resolve(list()));
    await page.settle();
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    page.fixture.detectChanges();
    page.component.marks.markAsDirty();
    const scrolled = vi.fn();
    const scrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrolled;
    onTestFinished(() => {
      Element.prototype.scrollIntoView = scrollIntoView;
    });
    page.button('← All grades').click();
    await page.settle();
    expect(page.component.selected()?.id).toBe('e-s1');
    expect(page.text()).toContain('Save or close the grade you are marking first.');
    // the refusal is brought into view beside the grade's buttons.
    expect(scrolled.mock.contexts[0]).toBe(page.root.querySelector('.grade-panel app-status'));
    page.button('Close').click();
    expect(page.component.selected()).toBeNull();
  });
});

describe('the address', () => {
  it('opens the grade it names once the grades load, and follows back and forward', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft'), grade('s2', 'finalized')]));
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/review?grade=e-s2');
    await page.settle();
    expect(page.component.viewing()?.id).toBe('e-s2');
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    await page.settle();
    expect(router.url).toBe('/review?grade=e-s1');
    await router.navigateByUrl('/review?grade=e-s2');
    await page.settle();
    expect(page.component.viewing()?.id).toBe('e-s2');
  });

  it('reads a grade not on the page, with its assessment', async () => {
    const other: Evaluation = { ...grade('s9', 'draft'), id: 'e-far', assessment_id: 'a2' };
    const evaluation = vi.fn(() => Promise.resolve(other));
    const page = render(Promise.resolve([grade('s1', 'draft')]), { evaluation });
    const assessments = TestBed.inject(AssessmentApi) as unknown as {
      get: (id: string) => Promise<Assessment>;
    };
    const get = vi.fn((id: string) =>
      Promise.resolve({ ...quiz, id, title: id === 'a2' ? 'Other quiz' : quiz.title }),
    );
    assessments.get = get;
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/review?grade=e-far');
    await page.settle();
    await page.settle();
    expect(evaluation).toHaveBeenCalledWith('e-far');
    expect(get).toHaveBeenCalledWith('a2');
    expect(page.component.selected()?.id).toBe('e-far');
    expect(page.panel()?.textContent).toContain('Other quiz');
    expect(page.panel()?.textContent).toContain('Half of 1?');
  });

  it('refuses a grade of another classroom', async () => {
    const other: Evaluation = { ...grade('s9', 'draft'), id: 'e-far', classroom_id: 'c2' };
    const page = render(Promise.resolve([grade('s1', 'draft')]), {
      evaluation: () => Promise.resolve(other),
    });
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/review?grade=e-far');
    await page.settle();
    await page.settle();
    expect(page.component.open()).toBeNull();
    expect(page.text()).toContain('The grade could not be opened.');
    expect(router.url).toBe('/review');
  });

  it('falls back to the newest grades for an assessment not in the classroom', async () => {
    const page = render(Promise.resolve([grade('s1', 'draft')]));
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/review?assessment=gone');
    await page.settle();
    await page.settle();
    expect(page.component.assessmentId()).toBe('');
    expect(page.text()).toContain('That assessment is not in this classroom.');
    expect(router.url).toBe('/review');
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
    expect(page.buttons(page.panel())).toContain('Finalize this grade');
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
    // the button keeps focus while the page loads.
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
    // a1 is one of the classroom's published assessments.
    (TestBed.inject(AssessmentApi) as unknown as { list: () => Promise<unknown> }).list = () =>
      Promise.resolve([{ id: 'a1', title: 'Fractions quiz', status: 'published' }]);
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

describe('coming back to grading review', () => {
  /* the page as created by the navigation to review, then left for another section. */
  async function leave(grades: () => Evaluation[]) {
    vi.useFakeTimers({ toFake: ['Date'] });
    let answer = grades;
    const page = render(Promise.resolve([]), {
      evaluations: vi.fn(() =>
        Promise.resolve({ evaluations: answer(), total: answer().length } as EvaluationPage),
      ),
    });
    const router = TestBed.inject(Router);
    await router.navigateByUrl('/review');
    await page.settle();
    await router.navigateByUrl('/home');
    return { ...page, router, answer: (next: () => Evaluation[]) => (answer = next) };
  }

  it('re-reads the grades in place, without showing loading, after 20 seconds away', async () => {
    const page = await leave(() => [grade('s1', 'draft')]);
    expect(page.card('Ana Lim')).toBeDefined();
    page.answer(() => [grade('s1', 'draft'), grade('s2', 'draft')]);
    vi.setSystemTime(Date.now() + 21_000);
    await page.router.navigateByUrl('/review');
    page.fixture.detectChanges();
    expect(page.text()).not.toContain('Loading grades…');
    expect(page.card('Ana Lim')).toBeDefined();
    await page.settle();
    expect(page.card('Ben Tan')).toBeDefined();
  });

  it('names a student enrolled while the teacher was away', async () => {
    const page = await leave(() => [grade('s1', 'draft')]);
    const identity = TestBed.inject(IdentityApi) as unknown as { roster: () => Promise<unknown> };
    identity.roster = () =>
      Promise.resolve([
        { id: 's1', username: 'ana', display_name: 'Ana Lim' },
        { id: 's3', username: 'cara', display_name: 'Cara Ong' },
      ]);
    page.answer(() => [grade('s1', 'draft'), grade('s3', 'draft')]);
    vi.setSystemTime(Date.now() + 21_000);
    await page.router.navigateByUrl('/review');
    await page.settle();
    expect(page.card('Cara Ong')).toBeDefined();
  });

  it('leaves a grade open for marking alone', async () => {
    const page = await leave(() => [grade('s1', 'draft')]);
    page.component.review(page.component.grades.value()?.grades[0] as Evaluation);
    const calls = (TestBed.inject(GradingApi).evaluations as ReturnType<typeof vi.fn>).mock.calls.length;
    vi.setSystemTime(Date.now() + 21_000);
    await page.router.navigateByUrl('/review');
    await page.settle();
    expect((TestBed.inject(GradingApi).evaluations as ReturnType<typeof vi.fn>).mock.calls.length).toBe(
      calls,
    );
    expect(page.component.selected()?.id).toBe('e-s1');
  });
});
