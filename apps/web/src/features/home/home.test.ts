// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { AnalyticsApi, AssessmentApi, GradingApi } from '../../api/clients';
import type { AssessmentSummary, Evaluation, Performance } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { SectionStateComponent } from '../../shared/section-state';
import { HomeComponent } from './home';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const assessment = (id: string, submitted: boolean): AssessmentSummary => ({
  id,
  title: `Quiz ${id}`,
  submitted,
  status: 'published',
  opens_at: null,
  due_at: null,
  late_policy: 'accept_flagged',
  question_count: 1,
  points: 1,
});
/** A result the student can see: Grading returns only finalized grades the teacher has released. */
const result = (assessmentId: string): Evaluation => ({
  id: `e-${assessmentId}`,
  assessment_id: assessmentId,
  classroom_id: 'c1',
  student_id: 'u1',
  status: 'finalized',
  evidence: [{ question_id: 'q1', topic_id: 'k1', correct: true, awarded: 1, possible: 1 }],
  answers: {},
  late: false,
  finalized_at: '2026-10-16T00:00:00Z',
  released_at: '2026-10-16T00:00:00Z',
});
const noEvidence: Performance = {
  student_id: 'u1',
  classroom_id: 'c1',
  evidence_status: 'no_finalized_evidence',
  interpretation: '',
  topics: [],
};

/** The student's Home for classroom c1, each part answering as given. */
function render(
  assessments: Promise<AssessmentSummary[]>,
  results: Promise<Evaluation[]>,
  performance: Promise<Performance> = Promise.resolve(noEvidence),
  need: () => Promise<void> = () => Promise.resolve(),
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: Session, useValue: { profile: signal({ id: 'u1', role: 'student' }) } },
      { provide: AssessmentApi, useValue: { list: () => assessments } },
      { provide: GradingApi, useValue: { mine: () => results } },
      { provide: AnalyticsApi, useValue: { performance: () => performance } },
      { provide: CurriculumNames, useValue: { need, topicName: () => '' } },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1'), classroomName: signal('3A') } },
    ],
  });
  TestBed.overrideComponent(HomeComponent, { set: { template: template('./home.html') } });
  TestBed.overrideComponent(SectionStateComponent, {
    set: { template: template('../../shared/section-state.html') },
  });
  const fixture = TestBed.createComponent(HomeComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  /** The text of the panel with this heading. */
  const panel = (heading: string) =>
    [...root.querySelectorAll('section')]
      .find((s) => s.querySelector('h3')?.textContent === heading)
      ?.textContent?.replace(/\s+/g, ' ')
      .trim() ?? '';
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { panel, settle };
}

describe('waiting for your teacher', () => {
  it('counts submitted assessments with no released result, so grades still in draft stay waiting', async () => {
    // q2 is submitted and its grade is a draft (or finalized but not released): Grading does not return it to the
    // student, so it is still waiting. q1 has a released result; q3 is not submitted.
    const page = render(
      Promise.resolve([assessment('q1', true), assessment('q2', true), assessment('q3', false)]),
      Promise.resolve([result('q1')]),
    );
    await page.settle();
    expect(page.panel('Waiting for your teacher')).toContain(
      "1 submission is waiting for your teacher's review.",
    );
    expect(page.panel('Recent results')).toContain('Quiz q1');
    expect(page.panel('To do')).toContain('Quiz q3');
    expect(page.panel('To do')).not.toContain('Quiz q2');
  });

  it('says nothing is waiting once every submission has a released result', async () => {
    const page = render(Promise.resolve([assessment('q1', true)]), Promise.resolve([result('q1')]));
    await page.settle();
    expect(page.panel('Waiting for your teacher')).toContain(
      'Nothing waiting. Every submission has a final grade.',
    );
    expect(page.panel('Waiting for your teacher')).not.toContain("waiting for your teacher's review");
  });

  it('waits for both lists before counting', async () => {
    const page = render(Promise.resolve([assessment('q1', true)]), new Promise(() => undefined));
    await page.settle();
    expect(page.panel('Waiting for your teacher')).toContain('Loading…');
    expect(page.panel('Waiting for your teacher')).not.toContain('Nothing waiting');
  });

  it('explains it cannot count when results fail, while the to-do list still shows', async () => {
    const page = render(
      Promise.resolve([assessment('q1', true), assessment('q2', false)]),
      Promise.reject(new Error('Grading is unavailable.')),
    );
    await page.settle();
    expect(page.panel('Waiting for your teacher')).toContain('could not be worked out');
    expect(page.panel('Recent results')).toContain('Grading is unavailable.');
    expect(page.panel('To do')).toContain('Quiz q2');
  });
});

describe('home empty states', () => {
  it('explains each empty part', async () => {
    const page = render(Promise.resolve([]), Promise.resolve([]));
    await page.settle();
    expect(page.panel('To do')).toContain('No assessments in this classroom yet.');
    expect(page.panel('Waiting for your teacher')).toContain('Nothing waiting.');
    expect(page.panel('Recent results')).toContain('No finalized results yet.');
    expect(page.panel('Topics to practise')).toContain('No finalized grades yet');
  });

  it('says when everything is submitted, and when one closed before the student submitted it', async () => {
    const closed: AssessmentSummary = {
      ...assessment('q2', false),
      due_at: '2026-01-01T00:00:00Z',
      late_policy: 'reject',
    };
    const done = render(Promise.resolve([assessment('q1', true)]), Promise.resolve([]));
    await done.settle();
    expect(done.panel('To do')).toContain('All caught up. You have submitted every assessment.');

    const missed = render(Promise.resolve([assessment('q1', true), closed]), Promise.resolve([]));
    await missed.settle();
    expect(missed.panel('To do')).toContain(
      'Nothing left to submit. 1 assessment closed before you submitted.',
    );
    expect(missed.panel('To do')).not.toContain('submitted every assessment');
  });
});

describe('topics to practise without the curriculum', () => {
  it('shows the topic ids instead of failing', async () => {
    const weak: Performance = {
      ...noEvidence,
      evidence_status: 'available',
      topics: [
        {
          topic_id: 'k1',
          attempted: 5,
          correct: 1,
          observed_accuracy: 0.2,
          evidence_status: 'observed',
          weakness_indicator: true,
        },
      ],
    };
    const page = render(Promise.resolve([]), Promise.resolve([]), Promise.resolve(weak), () =>
      Promise.reject(new Error('Curriculum is unavailable.')),
    );
    await page.settle();
    expect(page.panel('Topics to practise')).toContain('k1');
    expect(page.panel('Topics to practise')).toContain('1 of 5 correct');
    expect(page.panel('Topics to practise')).not.toContain('Curriculum is unavailable.');
  });
});
