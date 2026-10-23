// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { readFileSync } from 'node:fs';
import { of } from 'rxjs';
import { beforeAll, describe, expect, it } from 'vitest';
import { AnalyticsApi, IdentityApi, PerformanceAgentApi } from '../../api/clients';
import type { Interpretation, Performance, Student } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { FieldComponent } from '../../shared/field';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { InterpretationComponent } from './interpretation';
import { PerformanceComponent } from './performance';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const ana: Student = { id: 's1', username: 'ana', display_name: 'Ana Lim' };
const evidence = (topics: Performance['topics']): Performance => ({
  student_id: 's1',
  classroom_id: 'c1',
  evidence_status: topics.length ? 'available' : 'no_finalized_evidence',
  interpretation: '',
  topics,
});

/* a teacher's performance page for classroom c1, opened from a link to ana's evidence, whose roster and evidence load as given. */
function render(
  roster: Promise<Student[]>,
  performance: Promise<Performance>,
  student = 's1',
  need: () => Promise<void> = () => Promise.resolve(),
  interpret: () => Promise<Interpretation> = () => new Promise(() => undefined),
  role: 'teacher' | 'student' = 'teacher',
) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: {
          profile: signal({ id: role === 'teacher' ? 't1' : 's1' }),
          isTeacher: signal(role === 'teacher'),
          isStudent: signal(role === 'student'),
        },
      },
      { provide: IdentityApi, useValue: { roster: () => roster } },
      { provide: AnalyticsApi, useValue: { performance: () => performance } },
      { provide: PerformanceAgentApi, useValue: { interpret } },
      { provide: AiUsage, useValue: { refresh: () => undefined } },
      {
        provide: CurriculumNames,
        useValue: { need, topicName: (id: string) => (id === 'k1' ? 'Halves' : '') },
      },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
      { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({ student })) } },
    ],
  });
  TestBed.overrideComponent(PerformanceComponent, { set: { template: template('./performance.html') } });
  for (const [component, path] of [
    [InterpretationComponent, './interpretation.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(PerformanceComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent ?? '';
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const click = (selector: string) => (root.querySelector(selector) as HTMLButtonElement).click();
  return { text, settle, click };
}

describe('performance states', () => {
  it('shows the roster loading in the student field', async () => {
    const page = render(new Promise(() => undefined), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('Loading students…');
  });

  it('says when the classroom has no students to choose', async () => {
    const page = render(Promise.resolve([]), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('No students are enrolled in this classroom yet.');
  });

  it('shows the roster’s failure', async () => {
    const page = render(Promise.reject(new Error('Identity is unavailable.')), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('Identity is unavailable.');
  });

  it('shows the linked student’s evidence loading, then their topics', async () => {
    let resolve!: (p: Performance) => void;
    const page = render(Promise.resolve([ana]), new Promise((yes) => (resolve = yes)));
    await page.settle();
    expect(page.text()).toContain('Loading performance…');
    resolve(
      evidence([
        {
          topic_id: 'k1',
          attempted: 6,
          correct: 2,
          observed_accuracy: 2 / 6,
          evidence_status: 'observed',
          weakness_indicator: true,
        },
      ]),
    );
    await page.settle();
    expect(page.text()).not.toContain('Loading performance…');
    expect(page.text()).toContain('Halves');
    expect(page.text()).toContain('Needs practice');
  });

  it('explains a student with no finalized evidence', async () => {
    const page = render(Promise.resolve([ana]), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('No finalized assessment evidence for this student yet.');
  });

  it('shows the failure, not the empty text, when the evidence fails', async () => {
    const page = render(Promise.resolve([ana]), Promise.reject(new Error('Analytics is unavailable.')));
    await page.settle();
    expect(page.text()).toContain('Analytics is unavailable.');
    expect(page.text()).not.toContain('No finalized assessment evidence');
  });

  it('shows the evidence by topic id when the curriculum cannot be read', async () => {
    const metric = {
      topic_id: 'k9',
      attempted: 6,
      correct: 2,
      observed_accuracy: 2 / 6,
      evidence_status: 'observed' as const,
      weakness_indicator: true,
    };
    const page = render(Promise.resolve([ana]), Promise.resolve(evidence([metric])), 's1', () =>
      Promise.reject(new Error('Curriculum is unavailable.')),
    );
    await page.settle();
    expect(page.text()).toContain('k9');
    expect(page.text()).toContain('Needs practice');
    expect(page.text()).not.toContain('Curriculum is unavailable.');
  });
});

const halves = {
  topic_id: 'k1',
  attempted: 6,
  correct: 2,
  observed_accuracy: 2 / 6,
  marks_awarded: 2,
  marks_possible: 6,
  evidence_status: 'observed' as const,
  weakness_indicator: true,
};

const draft: Interpretation = {
  classroom_id: 'c1',
  student_id: 's1',
  observations: [{ text: 'Halves are where marks were lost.', evidence_ids: ['q1'] }],
  possible_misconceptions: [{ text: 'Adds the denominators.', evidence_ids: ['q1'] }],
  suggested_interventions: ['Model halving with fraction strips.'],
  recommended_topic_ids: ['k1'],
  evidence: [
    {
      question_id: 'q1',
      evaluation_id: 'e1',
      assessment_id: 'a1',
      assessment_title: 'Fractions quiz',
      topic_id: 'k1',
      prompt: 'What is half of 3/4?',
      awarded: 0,
      possible: 2,
    },
  ],
  steps: [{ tool: 'read_answers', reason: 'Halves look weak.', target: 'Fractions quiz', outcome: 'ok' }],
  model_turns: 2,
  dropped: { UNKNOWN_EVIDENCE: 1 },
  requires_teacher_review: true,
};

describe('performance interpretation', () => {
  it('offers the teacher an interpretation once the student’s evidence is on show', async () => {
    const page = render(Promise.resolve([ana]), Promise.resolve(evidence([halves])), 's1', undefined, () =>
      Promise.resolve(draft),
    );
    await page.settle();
    expect(page.text()).toContain('AI draft for your review');
    expect(page.text()).not.toContain('Halves are where marks were lost.');
    page.click('#interpret-button');
    await page.settle();
    expect(page.text()).toContain('Halves are where marks were lost.');
    expect(page.text()).toContain('Fractions quiz · What is half of 3/4? · 0 of 2 marks');
    expect(page.text()).toContain('Adds the denominators.');
    expect(page.text()).toContain('Model halving with fraction strips.');
    expect(page.text()).toContain('1 statement was left out');
    expect(page.text()).toContain('How the agent worked (1 step, 2 model turns)');
    expect(page.text()).toContain('Read answers: Fractions quiz (done). Why: Halves look weak.');
    expect(page.text()).toContain('Interpret again');
  });

  it('shows why an interpretation could not be drafted', async () => {
    const page = render(Promise.resolve([ana]), Promise.resolve(evidence([halves])), 's1', undefined, () =>
      Promise.reject(new Error("You've used today's AI allowance.")),
    );
    await page.settle();
    page.click('#interpret-button');
    await page.settle();
    expect(page.text()).toContain("You've used today's AI allowance.");
  });

  it('is for teachers only; students see that practice is not available yet', async () => {
    const page = render(
      Promise.resolve([]),
      Promise.resolve(evidence([halves])),
      's1',
      undefined,
      undefined,
      'student',
    );
    await page.settle();
    expect(page.text()).not.toContain('Interpret results');
    expect(page.text()).toContain('Practice unavailable');
  });
});
