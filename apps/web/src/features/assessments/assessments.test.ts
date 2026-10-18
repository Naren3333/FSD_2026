// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi, ContentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { AssessmentsComponent } from './assessments';
import { optionLines } from './editor';

describe('optionLines', () => {
  it('takes one trimmed option per non-blank line', () => {
    expect(optionLines(' 1/2 \n\n0.5\r\n  \n50%')).toEqual(['1/2', '0.5', '50%']);
  });
});

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
afterEach(() => vi.useRealTimers());

const QUESTION = 'q1';
const assessment = (change: Partial<Assessment> = {}): Assessment => ({
  id: 'a1',
  classroom_id: 'c1',
  title: 'Fractions',
  questions: [
    { id: QUESTION, prompt: 'Half of 1?', type: 'numerical', options: null, points: 1, topic_id: 's1' },
  ],
  due_at: null,
  late_policy: 'accept_flagged',
  ...change,
});
const summary: AssessmentSummary = {
  id: 'a1',
  title: 'Fractions',
  submitted: false,
  status: 'published',
  opens_at: null,
  due_at: null,
  late_policy: 'accept_flagged',
  question_count: 1,
  points: 1,
};

/** The assessments page's logic (no template) for a user with `role`, against a fake Assessment API. */
function setup(
  role: 'student' | 'teacher',
  open: Assessment = assessment(),
  saved: Record<string, string> = {},
) {
  const api = {
    list: vi.fn(() => Promise.resolve([summary])),
    get: vi.fn(() => Promise.resolve(open)),
    saved: vi.fn(() => Promise.resolve({ answers: saved, saved_at: null })),
    save: vi.fn((_id: string, answers: Record<string, string>) =>
      Promise.resolve({ answers, saved_at: '2026-10-17T02:00:00Z' }),
    ),
    submit: vi.fn(() => Promise.resolve({ id: 's1', status: 'submitted', late: false })),
  };
  const classroomId = signal('c1');
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: AssessmentApi, useValue: api },
      { provide: ContentApi, useValue: { documents: () => Promise.resolve([]) } },
      {
        provide: Session,
        useValue: { isStudent: signal(role === 'student'), isTeacher: signal(role === 'teacher') },
      },
      {
        provide: CurriculumNames,
        useValue: { topics: signal([]), refresh: () => Promise.resolve([]) },
      },
      { provide: ClassroomContext, useValue: { classroomId } },
      { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({})) } },
    ],
  });
  TestBed.overrideComponent(AssessmentsComponent, { set: { template: '', imports: [] } });
  const fixture = TestBed.createComponent(AssessmentsComponent);
  fixture.detectChanges();
  const settle = () => vi.advanceTimersByTimeAsync(0);
  return {
    page: fixture.componentInstance,
    api,
    classroomId,
    settle,
    unsaved: TestBed.inject(UnsavedChanges),
  };
}

describe('a student answering an assessment', () => {
  it('saves typing once it rests, and nothing more after the answers are submitted', async () => {
    vi.useFakeTimers();
    const { page, api, settle, unsaved } = setup('student');
    await settle();
    page.open('a1');
    await settle();
    page.answers.controls[QUESTION].setValue('0.5');
    page.answers.markAsDirty();
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.save).toHaveBeenLastCalledWith('a1', { [QUESTION]: '0.5' });
    expect(page.saveNote()).toMatch(/^Saved at /);

    page.submit();
    await settle();
    expect(page.submitted()).toBe(true);
    expect(page.answers.disabled).toBe(true);
    expect(unsaved.any()).toBe(false);
    // Nothing can be typed now; even a programmatic change starts no save.
    page.answers.controls[QUESTION].setValue('0.6');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
    expect(page.saveNote()).toBe('');
  });

  it('opens a closed assessment read only, with no saving', async () => {
    vi.useFakeTimers();
    const past = assessment({ due_at: '2020-01-01T00:00:00Z', late_policy: 'reject' });
    const { page, api, settle } = setup('student', past);
    await settle();
    page.open('a1');
    await settle();
    expect(page.answers.disabled).toBe(true);
    page.answers.controls[QUESTION].setValue('1');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).not.toHaveBeenCalled();
  });

  it('clears the answers by saving the empty state once, as asked, without a second save from the reset', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('student', assessment(), { [QUESTION]: '0.5' });
    await settle();
    page.open('a1');
    await settle();
    expect(page.answers.controls[QUESTION].value).toBe('0.5');
    page.clearAnswers();
    await settle();
    expect(api.save).toHaveBeenCalledExactlyOnceWith('a1', {});
    expect(page.answers.controls[QUESTION].value).toBe('');
    expect(page.saveNote()).toBe('Your answers are cleared.');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
  });

  it('keeps the answers when clearing fails to save', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('student', assessment(), { [QUESTION]: '0.5' });
    await settle();
    page.open('a1');
    await settle();
    api.save.mockRejectedValueOnce(new Error('Assessment is unavailable.'));
    page.clearAnswers();
    await settle();
    expect(page.answers.controls[QUESTION].value).toBe('0.5');
    expect(page.clearState.error()).toBe('Assessment is unavailable.');
  });

  it('shows no late save failure once the assessment is submitted', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('student');
    await settle();
    page.open('a1');
    await settle();
    let fail!: (error: Error) => void;
    api.save.mockImplementationOnce(() => new Promise((_, no) => (fail = no)));
    page.answers.controls[QUESTION].setValue('0.5');
    await vi.advanceTimersByTimeAsync(1500);
    page.submit();
    await settle();
    fail(new Error('offline'));
    await settle();
    expect(page.saveNote()).toBe('');
  });
});

describe('opening after the classroom changed', () => {
  it('drops an assessment that arrives after the user moved to another classroom', async () => {
    vi.useFakeTimers();
    const { page, api, classroomId, settle } = setup('student');
    await settle();
    let arrive!: (a: Assessment) => void;
    api.get.mockImplementationOnce(() => new Promise((yes) => (arrive = yes)));
    page.open('a1');
    classroomId.set('c2');
    TestBed.tick();
    arrive(assessment());
    await settle();
    expect(page.selected()).toBeNull();
  });
});

describe('a teacher previewing an assessment', () => {
  it('never counts preview answers as unsaved work', async () => {
    vi.useFakeTimers();
    const { page, settle, unsaved } = setup('teacher');
    await settle();
    page.open('a1');
    await settle();
    page.answers.controls[QUESTION].setValue('0.5');
    page.answers.markAsDirty();
    expect(unsaved.any()).toBe(false);
    page.confirmClear();
    expect(page.answers.controls[QUESTION].value).toBe('');
  });
});

describe('a teacher keeping generated questions', () => {
  it('fills the blank first question, then adds after it', async () => {
    vi.useFakeTimers();
    const { page, settle, unsaved } = setup('teacher');
    await settle();
    const generated = {
      prompt: 'Which test checks one function?',
      type: 'short_answer' as const,
      options: [],
      accepted_answers: ['unit test'],
      points: 1,
      topic_id: 's1',
      sources: [{ text: 'A unit test checks one function.' }],
      origin: 'generated' as const,
    };
    page.addGenerated(generated);
    expect(page.questions.length).toBe(1);
    expect(page.questions.at(0).controls.origin.value).toBe('generated');
    page.addGenerated({ ...generated, prompt: 'Name another test.' });
    expect(page.prompts()).toEqual(['Which test checks one function?', 'Name another test.']);
    expect(page.state.message()).toBe('Generated question added as question 2.');
    expect(unsaved.any()).toBe(true);
    expect(page.canSuggest()).toBe(true);
  });

  it('keeps a first question the teacher has started, even without wording', async () => {
    vi.useFakeTimers();
    const { page, settle } = setup('teacher');
    await settle();
    const first = page.questions.at(0).controls;
    first.type.setValue('multiple_choice');
    first.type.markAsDirty();
    page.addGenerated(generatedShortAnswer());
    expect(page.questions.length).toBe(2);
    expect(page.questions.at(0).controls.type.value).toBe('multiple_choice');
  });

  it('leaves out marking points the assessment has no room for', async () => {
    vi.useFakeTimers();
    const { page, settle } = setup('teacher');
    await settle();
    const full = Array.from({ length: 5 }, (_, n) => ({ text: `Point ${n}` }));
    for (let n = 0; n < 4; n++) page.addGenerated({ ...generatedShortAnswer(`Q${n}`), marking_scheme: full });
    expect(page.schemePassages()).toBe(20);
    page.addGenerated(generatedShortAnswer('One more'));
    expect(page.questions.at(4).controls.marking_scheme.value).toEqual([]);
    expect(page.state.message()).toContain('Its marking points were left out');
    expect(page.questions.hasError('schemeTotal')).toBe(false);
  });
});

function generatedShortAnswer(prompt = 'Which test checks one function?') {
  return {
    prompt,
    type: 'short_answer' as const,
    options: [],
    accepted_answers: ['unit test'],
    points: 1,
    topic_id: 's1',
    sources: [{ text: 'A unit test checks one function.' }],
    marking_scheme: [{ text: '1 mark: names a unit test.' }],
    origin: 'generated' as const,
  };
}
