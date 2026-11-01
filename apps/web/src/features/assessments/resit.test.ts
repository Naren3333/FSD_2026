// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, BankFilter, BankQuestion, Evaluation, Student, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { finalizedGrades, resitRefusal, resitRequest, strugglingStudents, weakTopics } from './resit';
import { ResitBuilderComponent } from './resit-builder';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/* the resit builder (adr 0017). */

const settle = () => new Promise((resolve) => setTimeout(resolve));

function grade(student: string, status: Evaluation['status'], marks: [string, string, number, number][]) {
  return {
    id: 'e-' + student,
    assessment_id: 'a1',
    classroom_id: 'c1',
    student_id: student,
    status,
    evidence: marks.map(([question_id, topic_id, awarded, possible]) => ({
      question_id,
      topic_id,
      awarded,
      possible,
      correct: awarded === possible,
    })),
    answers: {},
    late: false,
    submitted_at: null,
    release_policy: 'on_finalize',
    finalized_at: null,
    released_at: null,
  } satisfies Evaluation;
}

const original: Assessment = {
  id: 'a1',
  classroom_id: 'c1',
  title: 'Fractions',
  status: 'published',
  due_at: null,
  late_policy: 'accept_flagged',
  questions: [
    { id: 'q1', prompt: 'Half of 1?', type: 'numerical', options: null, points: 1, topic_id: 't1' },
    { id: 'q2', prompt: 'A third of 3?', type: 'numerical', options: null, points: 1, topic_id: 't2' },
  ],
};

const banked = (id: string, change: Partial<BankQuestion> = {}): BankQuestion => ({
  id,
  prompt: 'Bank ' + id,
  type: 'numerical',
  options: null,
  answer: '1',
  tolerance: '0',
  points: 1,
  topic_id: 't1',
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
  ...change,
});

const roster: Student[] = [
  { id: 's1', username: 'ana', display_name: 'Ana' },
  { id: 's2', username: 'ben', display_name: 'Ben' },
  { id: 's3', username: 'cy', display_name: 'Cy' },
];

describe('suggestions from grades', () => {
  it('reads up to five pages of 100 and keeps only finalized grades', async () => {
    const fetch = vi.fn((offset: number) =>
      Promise.resolve({
        evaluations: [grade('s' + offset, offset ? 'finalized' : 'draft', [])],
        total: 2,
      }),
    );
    const read = await finalizedGrades(fetch);
    expect(fetch.mock.calls).toEqual([
      [0, 100],
      [1, 100],
    ]);
    expect(read.grades.map((g) => g.student_id)).toEqual(['s1']);
    expect(read.partial).toBe(false);
  });

  it('says more remain past 500 results', async () => {
    const page = Array.from({ length: 100 }, (_, i) => grade('s' + i, 'finalized', []));
    const fetch = vi.fn(() => Promise.resolve({ evaluations: page, total: 640 }));
    const read = await finalizedGrades(fetch);
    expect(fetch).toHaveBeenCalledTimes(5);
    expect(fetch).toHaveBeenLastCalledWith(400, 100);
    expect(read.partial).toBe(true);
  });

  it('suggests the students who lost marks, and the topics where they did', () => {
    const grades = [
      grade('s1', 'finalized', [['q1', 't1', 1, 1]]),
      grade('s2', 'finalized', [
        ['q1', 't1', 0, 1],
        ['q2', '', 0, 1],
      ]),
    ];
    expect(strugglingStudents(grades)).toEqual(['s2']);
    // the evidence's topic, else its question's.
    expect(weakTopics(grades, new Set(['s2']), original.questions, new Set())).toEqual(['t1', 't2']);
    // without lost marks: the chosen questions' topics, else all of the original's.
    expect(weakTopics(grades, new Set(['s1']), original.questions, new Set(['q2']))).toEqual(['t2']);
    expect(weakTopics([], new Set(), original.questions, new Set())).toEqual(['t1', 't2']);
  });

  it('sends copies in the original order, and no title when left empty', () => {
    expect(resitRequest(['s1'], original.questions, new Set(['q2', 'q1']), ['b1'], '  ')).toEqual({
      student_ids: ['s1'],
      copy_question_ids: ['q1', 'q2'],
      bank_question_ids: ['b1'],
    });
    expect(resitRequest(['s1'], original.questions, new Set(), [], ' Again ').title).toBe('Again');
  });

  it('explains each refusal, and passes on any other failure', () => {
    for (const code of [
      'STUDENT_NOT_ENROLLED',
      'STUDENT_DELETED',
      'INVALID_BANK_QUESTION',
      'INVALID_TOPIC',
      'NOT_PUBLISHED',
      'ASSESSMENT_CHANGED',
    ])
      expect(resitRefusal(new ApiError('raw', 422, code))).not.toBe('raw');
    expect(resitRefusal(new ApiError('Assessment is unavailable.', 503))).toBe('Assessment is unavailable.');
  });
});

/* the builder's logic (no template) against fake apis. */
function setup(
  options: {
    grades?: Evaluation[];
    total?: number;
    bank?: (filter: BankFilter) => BankQuestion[];
    assessment?: Assessment;
    archived?: Topic[];
  } = {},
) {
  const grades = options.grades ?? [];
  const assessmentApi = {
    editable: vi.fn(() => Promise.resolve(options.assessment ?? original)),
    bankPage: vi.fn((filter: BankFilter) => {
      const items = (options.bank ?? (() => []))(filter);
      return Promise.resolve({ items, total: items.length });
    }),
    createResit: vi.fn((_id: string, request: { student_ids: string[] }) =>
      Promise.resolve({
        ...original,
        id: 'r1',
        status: 'draft',
        resit_of: 'a1',
        assignees: request.student_ids,
      }),
    ),
  };
  const gradingApi = {
    evaluations: vi.fn(() => Promise.resolve({ evaluations: grades, total: options.total ?? grades.length })),
  };
  const identityApi = { roster: vi.fn(() => Promise.resolve(roster)) };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: AssessmentApi, useValue: assessmentApi },
      { provide: GradingApi, useValue: gradingApi },
      { provide: IdentityApi, useValue: identityApi },
      {
        provide: CurriculumNames,
        useValue: {
          archived: signal(options.archived ?? []),
          topicName: (id: string) => 'Topic ' + id,
          need: () => Promise.resolve(),
        },
      },
    ],
  });
  TestBed.overrideComponent(ResitBuilderComponent, { set: { template: '', imports: [] } });
  const fixture = TestBed.createComponent(ResitBuilderComponent);
  const builder = fixture.componentInstance;
  const created: Assessment[] = [];
  builder.created.subscribe((a) => created.push(a));
  const open = async (id = 'a1') => {
    fixture.componentRef.setInput('assessmentId', id);
    fixture.componentRef.setInput('classroomId', 'c1');
    fixture.detectChanges();
    await settle();
    await settle();
  };
  return { builder, open, created, assessmentApi, gradingApi };
}

const lostOnQ1 = [
  grade('s1', 'finalized', [
    ['q1', 't1', 0, 1],
    ['q2', 't2', 1, 1],
  ]),
  grade('s2', 'finalized', [
    ['q1', 't1', 1, 1],
    ['q2', 't2', 1, 1],
  ]),
  // a draft shows no marks yet, so it suggests nothing.
  grade('s3', 'draft', [['q2', 't2', 0, 1]]),
];

describe('the resit builder', () => {
  it('pre-ticks the students who lost marks on finalized grades, and their questions', async () => {
    const { builder, open, gradingApi } = setup({ grades: lostOnQ1 });
    await open();
    expect(gradingApi.evaluations).toHaveBeenCalledWith('c1', 'a1', 0, 100);
    expect([...builder.students()]).toEqual(['s1']);
    expect([...builder.copies()]).toEqual(['q1']);
    expect(builder.topics()).toEqual(['t1']);
    expect(builder.marksOf('s1')).toBe('1 of 2 marks');
    expect(builder.marksOf('s3')).toBe('');
  });

  it('says suggestions use the first 500 results when more remain', async () => {
    const { builder, open } = setup({ grades: lostOnQ1, total: 900 });
    await open();
    expect(builder.grades.value()?.partial).toBe(true);
  });

  it('refuses an assessment that is not published', async () => {
    const { builder, open, gradingApi } = setup({ assessment: { ...original, status: 'draft' } });
    await open();
    expect(builder.original.error()).toMatch(/Only a published assessment/);
    expect(gradingApi.evaluations).not.toHaveBeenCalled();
  });

  it('marks what one chosen student answered, and can hide it, but not for several', async () => {
    const { builder, open, assessmentApi } = setup({
      grades: lostOnQ1,
      bank: (filter) => [banked('b1', { answered: !!filter.studentId })],
    });
    await open();
    expect(assessmentApi.bankPage).toHaveBeenLastCalledWith(
      { topicId: 't1', classroomId: 'c1', studentId: 's1', excludeAnswered: false },
      0,
      20,
    );
    expect(builder.bank.value()?.[0].answered).toBe(true);
    builder.hideAnswered.setValue(true);
    expect(assessmentApi.bankPage.mock.lastCall?.[0]).toMatchObject({
      studentId: 's1',
      excludeAnswered: true,
    });
    builder.toggleStudent('s2', true);
    await settle();
    expect(builder.single()).toBe('');
    expect(assessmentApi.bankPage.mock.lastCall?.[0]).toMatchObject({
      studentId: '',
      excludeAnswered: false,
    });
  });

  it('keeps a topic the teacher picked, and the questions they ticked', async () => {
    const { builder, open, assessmentApi } = setup({ grades: lostOnQ1 });
    await open();
    builder.topic.setValue('');
    expect(assessmentApi.bankPage.mock.lastCall?.[0].topicId).toBe('');
    builder.toggleCopy('q2', true);
    builder.toggleStudent('s1', false);
    expect(builder.topic.value).toBe('');
    expect([...builder.copies()]).toEqual(['q1', 'q2']);
  });

  it('holds at most 100 students and 50 questions', async () => {
    const { builder, open } = setup();
    await open();
    builder.students.set(new Set(Array.from({ length: 100 }, (_, i) => 'x' + i)));
    builder.toggleStudent('s1', true);
    expect(builder.students().has('s1')).toBe(false);
    builder.banked.set(new Map(Array.from({ length: 49 }, (_, i) => ['b' + i, banked('b' + i)])));
    builder.toggleCopy('q1', true);
    builder.toggleCopy('q2', true);
    expect(builder.questionCount()).toBe(50);
    expect(builder.copies().has('q2')).toBe(false);
  });

  it('never adds a bank question on an archived topic', async () => {
    const { builder, open } = setup({ archived: [{ id: 't9', name: 'Old' } as Topic] });
    await open();
    builder.toggleBank(banked('b1', { topic_id: 't9' }), true);
    expect(builder.banked().size).toBe(0);
  });

  it('asks for a student and a question before creating', async () => {
    const { builder, open, assessmentApi } = setup();
    await open();
    builder.create();
    expect(builder.state.error()).toBe('Choose at least one student for the resit.');
    builder.toggleStudent('s1', true);
    builder.create();
    expect(builder.state.error()).toBe('Choose at least one question for the resit.');
    expect(assessmentApi.createResit).not.toHaveBeenCalled();
  });

  it('creates the draft and hands it to the page to open in the editor', async () => {
    const { builder, open, assessmentApi, created } = setup({ grades: lostOnQ1 });
    await open();
    builder.toggleBank(banked('b1'), true);
    builder.title.setValue('Fractions again');
    builder.create();
    await settle();
    expect(assessmentApi.createResit).toHaveBeenCalledWith('a1', {
      student_ids: ['s1'],
      copy_question_ids: ['q1'],
      bank_question_ids: ['b1'],
      title: 'Fractions again',
    });
    expect(created.map((a) => a.id)).toEqual(['r1']);
    expect(builder.state.message()).toBe('Resit draft created for 1 student.');
  });

  it('shows a refusal in place and keeps every choice', async () => {
    const { builder, open, assessmentApi, created } = setup({ grades: lostOnQ1 });
    await open();
    assessmentApi.createResit.mockRejectedValueOnce(new ApiError('changed', 409, 'ASSESSMENT_CHANGED'));
    builder.create();
    await settle();
    expect(builder.state.error()).toMatch(/questions changed since you opened it/);
    expect([...builder.students()]).toEqual(['s1']);
    expect([...builder.copies()]).toEqual(['q1']);
    expect(created).toEqual([]);
  });
});
