// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
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

/** The Grading review page for classroom c1, whose grades load as `grades` says. */
function render(grades: Promise<Evaluation[]>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: GradingApi, useValue: { evaluations: () => grades } },
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
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
    ],
  });
  TestBed.overrideComponent(ReviewComponent, { set: { template: template('./review.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
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
  return { fixture, text, settle, card, buttons };
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
