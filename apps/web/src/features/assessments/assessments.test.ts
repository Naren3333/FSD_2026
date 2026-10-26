// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { ActivatedRoute, Router, convertToParamMap, type ParamMap } from '@angular/router';
import { BehaviorSubject, Subject } from 'rxjs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi, ContentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Topic } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { AnswerSheetComponent } from './answer-sheet';
import { AssessmentsComponent } from './assessments';
import { fillForm, optionLines } from './editor';

describe('optionLines', () => {
  it('takes one trimmed option per non-blank line', () => {
    expect(optionLines(' 1/2 \n\n0.5\r\n  \n50%')).toEqual(['1/2', '0.5', '50%']);
  });
});

beforeAll(() => {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
  // jsdom has no scrolling.
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
});
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

/* the assessments page's logic (no template) for a user with `role`, against a fake assessment api, with the answer sheet (no template either) that opens its assessments. */
function setup(
  role: 'student' | 'teacher',
  open: Assessment = assessment(),
  saved: Record<string, string> = {},
) {
  const api = {
    async assessmentPage() {
      const items = await this.list();
      return { items, total: items.length };
    },
    list: vi.fn(() => Promise.resolve([summary])),
    get: vi.fn((id: string) =>
      Promise.resolve(id === open.id ? open : { ...open, id, title: 'Other ' + id }),
    ),
    saved: vi.fn(() => Promise.resolve({ answers: saved, saved_at: null })),
    save: vi.fn((_id: string, answers: Record<string, string>) =>
      Promise.resolve({ answers, saved_at: '2026-10-17T02:00:00Z' }),
    ),
    submit: vi.fn(() => Promise.resolve({ id: 's1', status: 'submitted', late: false })),
    submission: vi.fn(() =>
      Promise.resolve({ answers: { [QUESTION]: '0.5' }, submitted_at: '2026-10-16T01:00:00Z', late: true }),
    ),
    create: vi.fn((created: Partial<Assessment>) => Promise.resolve({ ...open, ...created, id: 'a9' })),
    update: vi.fn((_id: string, changed: unknown) => Promise.resolve(changed)),
    details: vi.fn((_id: string, changed: unknown) => Promise.resolve(changed)),
    remove: vi.fn((id: string) => Promise.resolve({ id })),
  };
  const classroomId = signal('c1');
  const archived = signal<Topic[]>([]);
  const params = new BehaviorSubject<ParamMap>(convertToParamMap({}));
  const route = { queryParamMap: params.asObservable() };
  const router = { navigate: vi.fn(() => Promise.resolve(true)), events: new Subject<unknown>() };
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
        useValue: {
          topics: signal([]),
          archived,
          refresh: () => Promise.resolve([]),
          need: () => Promise.resolve(),
        },
      },
      {
        provide: ClassroomContext,
        useValue: {
          classroomId,
          known: signal(true),
          loading: signal(false),
          error: signal(''),
          refresh: () => Promise.resolve(),
        },
      },
      { provide: ActivatedRoute, useValue: route },
      { provide: Router, useValue: router },
    ],
  });
  TestBed.overrideComponent(AssessmentsComponent, { set: { template: '', imports: [] } });
  TestBed.overrideComponent(AnswerSheetComponent, { set: { template: '', imports: [] } });
  const fixture = TestBed.createComponent(AssessmentsComponent);
  fixture.detectChanges();
  const sheetFixture = TestBed.createComponent(AnswerSheetComponent);
  sheetFixture.componentRef.setInput('assessments', fixture.componentInstance.assessments);
  sheetFixture.componentRef.setInput('state', fixture.componentInstance.state);
  sheetFixture.detectChanges();
  // the page's template, and so its static query, is left out.
  fixture.componentInstance.sheet = sheetFixture.componentInstance;
  const settle = () => vi.advanceTimersByTimeAsync(0);
  /* the address naming `open` (a string), or any query such as `{ edit }`, or the list's address. */
  const go = (query?: string | Record<string, string>) =>
    params.next(convertToParamMap(typeof query === 'string' ? { open: query } : (query ?? {})));
  return {
    page: fixture.componentInstance,
    sheet: sheetFixture.componentInstance,
    api,
    router,
    route,
    go,
    classroomId,
    archived,
    settle,
    unsaved: TestBed.inject(UnsavedChanges),
  };
}

describe('a student answering an assessment', () => {
  it('saves typing once it rests, and nothing more after the answers are submitted', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle, unsaved } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.save).toHaveBeenLastCalledWith('a1', { [QUESTION]: '0.5' });
    expect(sheet.saveNote()).toMatch(/^Saved at /);

    sheet.submit();
    await settle();
    expect(sheet.submitted()).toBe(true);
    expect(sheet.answers.disabled).toBe(true);
    expect(unsaved.any()).toBe(false);
    // nothing can be typed now; even a programmatic change starts no save.
    sheet.answers.controls[QUESTION].setValue('0.6');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
    expect(sheet.saveNote()).toBe('');
  });

  it('opens a closed assessment read only, with no saving', async () => {
    vi.useFakeTimers();
    const past = assessment({ due_at: '2020-01-01T00:00:00Z', late_policy: 'reject' });
    const { sheet, api, settle } = setup('student', past);
    await settle();
    sheet.open('a1');
    await settle();
    expect(sheet.answers.disabled).toBe(true);
    sheet.answers.controls[QUESTION].setValue('1');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).not.toHaveBeenCalled();
  });

  it('clears the answers by saving the empty state once, as asked, without a second save from the reset', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle } = setup('student', assessment(), { [QUESTION]: '0.5' });
    await settle();
    sheet.open('a1');
    await settle();
    expect(sheet.answers.controls[QUESTION].value).toBe('0.5');
    sheet.clearAnswers();
    await settle();
    expect(api.save).toHaveBeenCalledExactlyOnceWith('a1', {});
    expect(sheet.answers.controls[QUESTION].value).toBe('');
    expect(sheet.saveNote()).toBe('Your answers are cleared.');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
  });

  it('keeps the answers when clearing fails to save', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle } = setup('student', assessment(), { [QUESTION]: '0.5' });
    await settle();
    sheet.open('a1');
    await settle();
    api.save.mockRejectedValueOnce(new Error('Assessment is unavailable.'));
    sheet.clearAnswers();
    await settle();
    expect(sheet.answers.controls[QUESTION].value).toBe('0.5');
    expect(sheet.clearState.error()).toBe('Assessment is unavailable.');
  });

  it('shows no late save failure once the assessment is submitted', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    let fail!: (error: Error) => void;
    api.save.mockImplementationOnce(() => new Promise((_, no) => (fail = no)));
    sheet.answers.controls[QUESTION].setValue('0.5');
    await vi.advanceTimersByTimeAsync(1500);
    sheet.submit();
    await settle();
    fail(new Error('offline'));
    await settle();
    expect(sheet.saveNote()).toBe('');
  });
});

describe('a student opening an assessment they submitted', () => {
  it('shows their submitted answers, read only, with when they submitted and that it was late', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, settle } = setup('student');
    api.list.mockResolvedValue([{ ...summary, submitted: true }]);
    page.load();
    await settle();
    sheet.open('a1');
    await settle();
    expect(api.submission).toHaveBeenCalledWith('a1');
    expect(api.saved).not.toHaveBeenCalled();
    expect(sheet.answers.getRawValue()).toEqual({ [QUESTION]: '0.5' });
    expect(sheet.answers.disabled).toBe(true);
    expect(sheet.submission()).toEqual({
      answers: { [QUESTION]: '0.5' },
      submitted_at: '2026-10-16T01:00:00Z',
      late: true,
    });
    expect(sheet.saveNote()).toBe('');
  });

  it('opens it empty when Assessment has no submission after all', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, settle } = setup('student');
    api.list.mockResolvedValue([{ ...summary, submitted: true }]);
    api.submission.mockRejectedValueOnce(new ApiError('You have not submitted this assessment.', 404));
    page.load();
    await settle();
    sheet.open('a1');
    await settle();
    expect(page.state.error()).toBe('');
    expect(sheet.submission()).toBeNull();
    expect(sheet.answers.getRawValue()).toEqual({ [QUESTION]: '' });
  });

  it('keeps what they just submitted to show', async () => {
    vi.useFakeTimers();
    const { sheet, settle } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.submit();
    await settle();
    expect(sheet.submission()?.answers).toEqual({ [QUESTION]: '0.5' });
    expect(sheet.submission()?.late).toBe(false);
  });
});

describe('a student whose autosave finds the assessment changed', () => {
  it('asks them to open it again, as a submission would, and saves no more', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    api.save.mockRejectedValueOnce(
      new ApiError('Your teacher changed this assessment.', 409, 'ASSESSMENT_CHANGED'),
    );
    sheet.answers.controls[QUESTION].setValue('0.5');
    await vi.advanceTimersByTimeAsync(1500);
    expect(sheet.changed()).toBe(true);
    expect(sheet.saveNote()).toMatch(/^This assessment changed since you opened it/);
    sheet.answers.controls[QUESTION].setValue('0.6');
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
  });
});

describe('a student answering as the due date passes', () => {
  it('closes Submit when a reject-late due time passes while the assessment is open', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-16T09:59:00Z'));
    const due = assessment({ due_at: '2026-10-16T10:00:00Z', late_policy: 'reject' });
    const { sheet, settle } = setup('student', due);
    await settle();
    sheet.open('a1');
    await settle();
    expect(sheet.due()).toBe('upcoming');
    await vi.advanceTimersByTimeAsync(90 * 1000);
    expect(sheet.due()).toBe('closed');
  });

  it('checks the due date again at Submit', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-16T09:59:59Z'));
    const due = assessment({ due_at: '2026-10-16T10:00:00Z', late_policy: 'reject' });
    const { page, sheet, api, settle } = setup('student', due);
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    vi.setSystemTime(new Date('2026-10-16T10:00:05Z'));
    sheet.submit();
    await settle();
    expect(api.submit).not.toHaveBeenCalled();
    expect(page.state.error()).toBe('This assessment is past its due date and no longer takes submissions.');
  });
});

describe('a student submitting an assessment the teacher changed', () => {
  it('asks them to open it again, and keeps their answers to the questions still there', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, settle, unsaved } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    api.submit.mockRejectedValueOnce(
      new ApiError('Your teacher changed this assessment.', 409, 'ASSESSMENT_CHANGED'),
    );
    sheet.submit();
    await settle();
    expect(page.state.error()).toBe(
      'This assessment changed since you opened it. Open it again to see the current questions; your answers ' +
        'to questions that did not change are kept.',
    );
    expect(sheet.changed()).toBe(true);
    expect(sheet.submitted()).toBe(false);

    const added = { id: 'q2', prompt: 'Half of 4?', type: 'numerical' as const, options: null, points: 1 };
    api.get.mockResolvedValueOnce(
      assessment({ questions: [...assessment().questions, { ...added, topic_id: 's1' }] }),
    );
    sheet.reopen();
    await settle();
    expect(sheet.changed()).toBe(false);
    expect(sheet.answers.getRawValue()).toEqual({ [QUESTION]: '0.5', q2: '' });
    expect(unsaved.any()).toBe(true);
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.save).toHaveBeenLastCalledWith('a1', { [QUESTION]: '0.5' });
  });

  it('shows any other refusal as the server explains it', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, settle } = setup('student');
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    api.submit.mockRejectedValueOnce(new ApiError('This assessment is closed.', 409, 'CLOSED'));
    sheet.submit();
    await settle();
    expect(page.state.error()).toBe('This assessment is closed.');
    expect(sheet.changed()).toBe(false);
  });
});

describe('opening after the classroom changed', () => {
  it('drops an assessment that arrives after the user moved to another classroom', async () => {
    vi.useFakeTimers();
    const { sheet, api, classroomId, settle } = setup('student');
    await settle();
    let arrive!: (a: Assessment) => void;
    api.get.mockImplementationOnce(() => new Promise((yes) => (arrive = yes)));
    sheet.open('a1');
    classroomId.set('c2');
    TestBed.tick();
    arrive(assessment());
    await settle();
    expect(sheet.selected()).toBeNull();
  });
});

describe('a student opening an assessment by its address', () => {
  it('opens it from the list by moving to its address', async () => {
    vi.useFakeTimers();
    const { page, router, route, settle } = setup('student');
    await settle();
    page.openFromList('a1');
    expect(router.navigate).toHaveBeenCalledWith([], { relativeTo: route, queryParams: { open: 'a1' } });
  });

  it('opens the assessment the address names, and leaves it when the address no longer does', async () => {
    vi.useFakeTimers();
    const { page, sheet, go, settle } = setup('student');
    await settle();
    go('a1');
    await settle();
    expect(sheet.selected()?.id).toBe('a1');
    expect(page.studentViewing()).toBe(true);
    go();
    await settle();
    expect(sheet.selected()).toBeNull();
    expect(page.studentViewing()).toBe(false);
  });

  it('saves a waiting save as the student leaves, and leaves nothing unsaved', async () => {
    vi.useFakeTimers();
    const { sheet, api, go, settle, unsaved } = setup('student');
    await settle();
    go('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    expect(unsaved.any()).toBe(true);
    go();
    await settle();
    expect(api.save).toHaveBeenCalledExactlyOnceWith('a1', { [QUESTION]: '0.5' });
    expect(sheet.selected()).toBeNull();
    expect(unsaved.any()).toBe(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(api.save).toHaveBeenCalledTimes(1);
  });

  it('keeps the student on the assessment, at its address, when the last save fails twice', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, router, route, go, settle, unsaved } = setup('student');
    await settle();
    go('a1');
    await settle();
    api.save.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'));
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    go();
    await settle();
    expect(api.save).toHaveBeenCalledTimes(2);
    expect(sheet.selected()?.id).toBe('a1');
    expect(sheet.answers.controls[QUESTION].value).toBe('0.5');
    expect(sheet.answers.enabled).toBe(true);
    expect(unsaved.any()).toBe(true);
    expect(page.state.error()).toMatch(
      /^Your latest answers could not be saved, so the assessment is still open/,
    );
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: { open: 'a1' },
      replaceUrl: true,
    });
    // leaving again tries again, and the failure no longer shows once it works.
    go();
    await settle();
    expect(api.save).toHaveBeenCalledTimes(3);
    expect(sheet.selected()).toBeNull();
    expect(page.state.error()).toBe('');
  });

  it('lets the student leave when the server refuses their answers for good, and says so on the list', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, router, go, settle, unsaved } = setup('student');
    await settle();
    go('a1');
    await settle();
    api.save.mockRejectedValue(new ApiError('Assessment not found.', 404));
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    router.navigate.mockClear();
    go();
    await settle();
    expect(sheet.selected()).toBeNull();
    expect(unsaved.any()).toBe(false);
    expect(page.state.error()).toBe(
      'Your latest answers to Fractions could not be saved. Assessment not found.',
    );
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it('opens another assessment the address names in place of the open one, keeping its answers saved', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, go, settle } = setup('student');
    api.list.mockResolvedValue([summary, { ...summary, id: 'a2', title: 'Other a2' }]);
    page.load();
    await settle();
    go('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    go('a2');
    await settle();
    expect(api.save).toHaveBeenCalledWith('a1', { [QUESTION]: '0.5' });
    expect(sheet.selected()?.id).toBe('a2');
    expect(page.state.error()).toBe('');
  });

  it('opens a deep link to an assessment that is not on the list page', async () => {
    vi.useFakeTimers();
    const { sheet, api, go, settle } = setup('student');
    await settle();
    go('a3');
    await settle();
    expect(api.get).toHaveBeenCalledWith('a3');
    expect(sheet.selected()?.id).toBe('a3');
  });

  it('goes back to the list address when the assessment cannot be read', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, router, route, go, settle } = setup('student');
    await settle();
    api.get.mockRejectedValueOnce(new Error('Assessment not found.'));
    go('a3');
    await settle();
    expect(sheet.selected()).toBeNull();
    expect(page.state.error()).toBe('Assessment not found.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });

  it('says it could not open an assessment of another classroom, and goes back to the list address', async () => {
    vi.useFakeTimers();
    const { page, sheet, api, router, route, go, settle } = setup('student');
    await settle();
    api.get.mockResolvedValueOnce(assessment({ id: 'a3', classroom_id: 'c9' }));
    go('a3');
    await settle();
    expect(sheet.selected()).toBeNull();
    expect(page.state.error()).toBe('The assessment could not be opened.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });

  it('goes back to the list address when the classroom changes', async () => {
    vi.useFakeTimers();
    const { router, route, classroomId, go, settle } = setup('student');
    await settle();
    go('a1');
    await settle();
    classroomId.set('c2');
    TestBed.tick();
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });
});

describe('a teacher previewing an assessment', () => {
  it('never counts preview answers as unsaved work', async () => {
    vi.useFakeTimers();
    const { sheet, settle, unsaved } = setup('teacher');
    await settle();
    sheet.open('a1');
    await settle();
    sheet.answers.controls[QUESTION].setValue('0.5');
    sheet.answers.markAsDirty();
    expect(unsaved.any()).toBe(false);
    sheet.confirmClear();
    expect(sheet.answers.controls[QUESTION].value).toBe('');
  });
});

describe('a teacher moving through the editor', () => {
  const valid = (prompt: string) => ({ ...generatedShortAnswer(), prompt });

  it('keeps one question open, but never folds one with an error', async () => {
    vi.useFakeTimers();
    const { page, settle } = setup('teacher');
    await settle();
    page.addGenerated(valid('First?'));
    page.addGenerated(valid('Second?'));
    page.addGenerated(valid('Third?'));
    const [first, second, third] = page.questions.controls;
    page.goToQuestion(1);
    expect(page.isCollapsed(first)).toBe(true);
    expect(page.isCollapsed(second)).toBe(false);
    expect(page.isCollapsed(third)).toBe(true);
    // an emptied prompt is an error, so that question stays open when another opens.
    first.controls.prompt.setValue('');
    page.goToQuestion(2);
    expect(page.isCollapsed(first)).toBe(false);
    expect(page.isCollapsed(second)).toBe(true);
    expect(page.isCollapsed(third)).toBe(false);
  });

  it('says the due date in the save bar, or that there is none', async () => {
    vi.useFakeTimers();
    const { page, settle } = setup('teacher');
    await settle();
    expect(page.dueLine()).toBe('No due date');
    page.form.controls.due_at.setValue('2026-11-03T17:00');
    expect(page.dueLine()).toMatch(/^Due 3 Nov 2026/);
    page.form.controls.due_at.setValue('not a date');
    expect(page.dueLine()).toBe('No due date');
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
    expect(page.markingPoints()).toBe(20);
    page.addGenerated(generatedShortAnswer('One more'));
    expect(page.questions.at(4).controls.marking_scheme.value).toEqual([]);
    expect(page.state.message()).toContain('Its marking points were left out');
    expect(page.questions.hasError('schemeTotal')).toBe(false);
  });
});

describe('a teacher changing a published assessment', () => {
  const published = assessment({
    status: 'published',
    release_policy: 'on_finalize',
    ai_marking: false,
    questions: [
      {
        id: QUESTION,
        prompt: 'Half of 1?',
        type: 'numerical',
        options: [],
        answer: '0.5',
        tolerance: '0',
        points: 1,
        topic_id: 's1',
      },
    ],
  });

  it('replaces it while nobody has submitted, keeping it published', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.edit({ ...summary, submissions: 0 });
    await settle();
    expect(page.editingPublished()).toBe(true);
    expect(page.detailsOnly()).toBe(false);
    page.questions.at(0).controls.prompt.setValue('Half of 2?');
    page.save(false);
    await settle();
    expect(api.update).toHaveBeenCalledWith(
      'a1',
      expect.objectContaining({
        status: 'published',
        questions: [expect.objectContaining({ prompt: 'Half of 2?' })],
      }),
    );
    expect(api.details).not.toHaveBeenCalled();
    expect(page.state.message()).toBe('Fractions saved.');
    expect(page.editing()).toBeNull();
  });

  it('changes only its details once students have submitted', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.edit({ ...summary, submissions: 2 });
    await settle();
    expect(page.detailsOnly()).toBe(true);
    expect(page.questions.disabled).toBe(true);
    page.form.controls.title.setValue('Halves');
    page.form.controls.late_policy.setValue('reject');
    page.save(false);
    await settle();
    expect(api.details).toHaveBeenCalledWith('a1', { title: 'Halves', due_at: null, late_policy: 'reject' });
    expect(api.update).not.toHaveBeenCalled();
    // stopping leaves the editor whole again for the next assessment.
    expect(page.questions.enabled).toBe(true);
    expect(page.detailsOnly()).toBe(false);
  });

  it('keeps only the details when a student submits while the teacher edits', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.edit({ ...summary, submissions: 0 });
    await settle();
    api.update.mockRejectedValueOnce(new Error('Students have submitted this assessment.'));
    api.list.mockResolvedValue([{ ...summary, submissions: 1 }]);
    page.save(false);
    await settle();
    expect(page.state.error()).toBe('Students have submitted this assessment.');
    expect(page.editing()?.id).toBe('a1');
    expect(page.detailsOnly()).toBe(true);
  });

  it('deletes it after the teacher confirms', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.confirmDelete(summary);
    page.remove();
    await settle();
    expect(api.remove).toHaveBeenCalledWith('a1', false);
    expect(page.state.message()).toBe('Fractions deleted.');
  });

  it('closes its preview when it is the one open', async () => {
    vi.useFakeTimers();
    const { page, sheet, settle } = setup('teacher', published);
    page.sheet = sheet;
    await settle();
    sheet.open('a1');
    await settle();
    expect(sheet.selected()?.id).toBe('a1');
    page.confirmDelete(summary);
    page.remove();
    await settle();
    expect(sheet.selected()).toBeNull();
  });

  it('deletes it with its grades once students have submitted, as the dialog says', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.confirmDelete({ ...summary, submissions: 2 });
    page.remove();
    await settle();
    expect(api.remove).toHaveBeenCalledWith('a1', true);
  });

  it('tells the teacher what deleting takes when a student submitted meanwhile', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    api.remove.mockRejectedValueOnce(new Error('Students have submitted this assessment.'));
    api.list.mockResolvedValue([{ ...summary, submissions: 1 }]);
    page.confirmDelete({ ...summary, submissions: 0 });
    page.remove();
    await settle();
    expect(page.deleteState.error()).toBe('Students have submitted this assessment.');
    expect(page.deleting()?.submissions).toBe(1);
  });

  it('keeps only the details when only students since removed from the class have submitted', async () => {
    vi.useFakeTimers();
    const { page, settle } = setup('teacher', published);
    await settle();
    page.edit({ ...summary, submissions: 0, has_submissions: true });
    await settle();
    expect(page.detailsOnly()).toBe(true);
    expect(page.questions.disabled).toBe(true);
  });

  it('deletes it with its grades when only students since removed from the class have submitted', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.confirmDelete({ ...summary, submissions: 0, has_submissions: true });
    expect(page.deletingSubmitted()).toBe(true);
    page.remove();
    await settle();
    expect(api.remove).toHaveBeenCalledWith('a1', true);
  });

  it('keeps only the details when the server refuses a replacement because of submissions', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    page.edit({ ...summary, submissions: 0 });
    await settle();
    // the list cannot tell: the only submitter is no longer enrolled.
    api.update.mockRejectedValueOnce(new ApiError('Students have submitted.', 409, 'HAS_SUBMISSIONS'));
    page.save(false);
    await settle();
    expect(page.detailsOnly()).toBe(true);
    expect(page.state.error()).toContain('only its title, due date and late policy can change');
    page.form.controls.title.setValue('Halves');
    page.save(false);
    await settle();
    expect(api.details).toHaveBeenCalledWith('a1', expect.objectContaining({ title: 'Halves' }));
  });

  it('asks again with the grades when the server refuses a delete because of submissions', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', published);
    await settle();
    api.remove.mockRejectedValueOnce(new ApiError('Students have submitted.', 409, 'HAS_SUBMISSIONS'));
    page.confirmDelete({ ...summary, submissions: 0 });
    page.remove();
    await settle();
    expect(page.deleteState.error()).toBe('Students have submitted.');
    expect(page.deletingSubmitted()).toBe(true);
    page.remove();
    await settle();
    expect(api.remove).toHaveBeenLastCalledWith('a1', true);
  });
});

describe('a teacher whose refused delete cannot re-read the list', () => {
  it('asks again with the grades without saying who submitted', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', assessment({ status: 'published' }));
    await settle();
    api.remove.mockRejectedValueOnce(new ApiError('Students have submitted.', 409, 'HAS_SUBMISSIONS'));
    api.list.mockRejectedValueOnce(new Error('Assessment is unavailable.'));
    page.confirmDelete({ ...summary, submissions: 0 });
    page.remove();
    await settle();
    expect(page.deletingSubmitted()).toBe(true);
    expect(page.deleting()?.submissions).toBeUndefined();
  });
});

describe('a teacher editing one assessment after another', () => {
  it('enables the whole editor for a draft after editing the details of a submitted assessment', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher', assessment({ status: 'published' }));
    await settle();
    page.edit({ ...summary, submissions: 2 });
    await settle();
    const c = page.form.controls;
    expect(page.detailsOnly()).toBe(true);
    expect([c.opens_at, c.release_policy, c.ai_marking, c.questions].map((x) => x.disabled)).toEqual([
      true,
      true,
      true,
      true,
    ]);

    const draft = assessment({
      id: 'a2',
      status: 'draft',
      questions: [
        {
          id: 'q2',
          prompt: 'Name a test.',
          type: 'short_answer',
          options: null,
          accepted_answers: ['unit test'],
          points: 1,
          topic_id: 's1',
        },
      ],
    });
    api.get.mockResolvedValueOnce(draft);
    page.edit({ ...summary, id: 'a2', status: 'draft', submissions: undefined });
    await settle();
    expect(page.editing()?.id).toBe('a2');
    expect(page.detailsOnly()).toBe(false);
    expect([c.opens_at, c.release_policy, c.ai_marking, c.questions].map((x) => x.enabled)).toEqual([
      true,
      true,
      true,
      true,
    ]);
    // only the controls its question type uses are on.
    expect(page.questions.at(0).controls.accepted.enabled).toBe(true);
    expect(page.questions.at(0).controls.answer.enabled).toBe(false);
  });
});

describe('a teacher editing a question whose topic is archived', () => {
  it('names the archived topic and saves only once an active one is chosen', async () => {
    vi.useFakeTimers();
    const question = { ...assessment().questions[0], answer: '0.5', tolerance: '0' };
    const { page, api, archived, settle } = setup(
      'teacher',
      assessment({ status: 'draft', questions: [question] }),
    );
    archived.set([{ id: 's1', name: 'Old fractions' }]);
    await settle();
    page.edit({ ...summary, status: 'draft' });
    await settle();
    const topic = page.questions.at(0).controls.topic_id;
    expect(page.archivedTopic(topic.value)).toBe('Old fractions');
    expect(topic.hasError('archived')).toBe(true);
    expect(topic.touched).toBe(true);
    page.save(false);
    await settle();
    expect(api.update).not.toHaveBeenCalled();
    // a question added after it does not take the archived topic.
    page.addQuestion();
    expect(page.questions.at(1).controls.topic_id.value).toBe('');
    page.removeQuestion(1);

    topic.setValue('s2');
    expect(topic.valid).toBe(true);
    page.save(false);
    await settle();
    expect(api.update).toHaveBeenCalledWith(
      'a1',
      expect.objectContaining({ questions: [expect.objectContaining({ topic_id: 's2' })] }),
    );
  });
});

describe('a teacher deleting an assessment when the list cannot be re-read', () => {
  it('announces the deletion and says the list is out of date on the page', async () => {
    vi.useFakeTimers();
    const { page, api, settle } = setup('teacher');
    await settle();
    page.confirmDelete(summary);
    api.list.mockRejectedValueOnce(new Error('Assessment is unavailable.'));
    page.remove();
    await settle();
    expect(page.deleting()).toBeNull();
    expect(page.deleteState.error()).toBe('');
    expect(page.state.message()).toBe('Fractions deleted.');
    expect(page.state.error()).toBe('The list could not be refreshed. Assessment is unavailable.');
    expect(page.assessments.value()).toEqual([]);
  });
});

describe('a teacher moving between the list, the editor and a preview', () => {
  const draft = assessment({
    status: 'draft',
    questions: [{ ...assessment().questions[0], answer: '0.5', tolerance: '0' }],
  });

  it('edits the assessment its address names once the list has it, and only once', async () => {
    vi.useFakeTimers();
    const { page, api, go, settle } = setup('teacher', draft);
    // before the list has loaded.
    go({ edit: 'a1' });
    await settle();
    expect(page.view()).toBe('edit');
    expect(page.editing()?.id).toBe('a1');
    expect(api.get).toHaveBeenCalledTimes(1);
    // back and forward, or the address of a created draft, name the one being edited.
    go({ edit: 'a1' });
    await settle();
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('keeps unsaved work at its own address when another assessment is asked for', async () => {
    vi.useFakeTimers();
    const { page, router, route, go, settle } = setup('teacher', draft);
    await settle();
    page.form.controls.title.setValue('Halves');
    page.form.markAsDirty();
    go({ edit: 'a1' });
    await settle();
    expect(page.editing()).toBeNull();
    expect(page.state.error()).toBe('Save or clear the assessment you are editing first.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: { edit: 'new' },
      replaceUrl: true,
    });
  });

  it('says it could not open an assessment that is not on the list, and goes back to the list', async () => {
    vi.useFakeTimers();
    const { page, api, router, route, go, settle } = setup('teacher', draft);
    await settle();
    go({ edit: 'a7' });
    await settle();
    expect(api.get).not.toHaveBeenCalled();
    expect(page.state.error()).toBe('The assessment could not be opened.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });

  it('gives a created draft its own address, in place of the new assessment', async () => {
    vi.useFakeTimers();
    const { page, router, route, go, settle } = setup('teacher', draft);
    await settle();
    go({ edit: 'new' });
    await settle();
    fillForm(page.form, draft);
    page.save(false);
    await settle();
    expect(page.editing()?.id).toBe('a9');
    expect(page.state.message()).toBe('Draft created.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: { edit: 'a9' },
      replaceUrl: true,
    });
  });

  it('goes back to the list after publishing, in place of the editor', async () => {
    vi.useFakeTimers();
    const { page, api, router, route, settle } = setup('teacher', draft);
    await settle();
    fillForm(page.form, draft);
    page.save(true);
    await settle();
    expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ status: 'published' }));
    expect(page.editing()).toBeNull();
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });

  it('goes back to the list after saving changes to a published assessment', async () => {
    vi.useFakeTimers();
    const { page, router, route, settle } = setup('teacher', { ...draft, status: 'published' });
    await settle();
    await page.edit({ ...summary, submissions: 0 });
    page.save(false);
    await settle();
    expect(page.state.message()).toBe('Fractions saved.');
    expect(router.navigate).toHaveBeenLastCalledWith([], {
      relativeTo: route,
      queryParams: {},
      replaceUrl: true,
    });
  });

  it('opens the preview its address names, and closes it when the address no longer does', async () => {
    vi.useFakeTimers();
    const { page, sheet, go, settle } = setup('teacher', draft);
    await settle();
    go({ preview: 'a1' });
    await settle();
    expect(page.view()).toBe('preview');
    expect(sheet.selected()?.id).toBe('a1');
    go();
    await settle();
    expect(page.view()).toBe('list');
    expect(sheet.selected()).toBeNull();
    // a teacher's open link, such as from home, is their preview.
    go('a1');
    await settle();
    expect(page.view()).toBe('preview');
    expect(sheet.selected()?.id).toBe('a1');
  });

  it('goes back to the list address when the classroom changes on an assessment', async () => {
    vi.useFakeTimers();
    for (const query of [{ edit: 'a1' }, { preview: 'a1' }]) {
      const { router, route, classroomId, go, settle } = setup('teacher', draft);
      await settle();
      go(query);
      await settle();
      router.navigate.mockClear();
      classroomId.set('c2');
      TestBed.tick();
      expect(router.navigate).toHaveBeenLastCalledWith([], {
        relativeTo: route,
        queryParams: {},
        replaceUrl: true,
      });
    }
  });

  it('keeps a new assessment at its address when the classroom changes', async () => {
    vi.useFakeTimers();
    const { router, classroomId, go, settle } = setup('teacher', draft);
    await settle();
    go({ edit: 'new' });
    await settle();
    router.navigate.mockClear();
    classroomId.set('c2');
    TestBed.tick();
    expect(router.navigate).not.toHaveBeenCalled();
  });

  it("returns focus to the list's Preview assessment button when the preview's own Close preview closes it", async () => {
    vi.useFakeTimers();
    const { page, sheet, router, route, go, settle } = setup('teacher', draft);
    await settle();
    const toggle = document.body.appendChild(document.createElement('button'));
    toggle.id = 'preview-a1';
    try {
      go({ preview: 'a1' });
      await settle();
      page.previewClosed('a1');
      expect(router.navigate).toHaveBeenLastCalledWith([], { relativeTo: route, queryParams: {} });
      go();
      await settle();
      expect(sheet.selected()).toBeNull();
      expect(document.activeElement).toBe(toggle);
    } finally {
      toggle.remove();
    }
  });

  it('moves focus to the preview heading once the preview opens', async () => {
    vi.useFakeTimers();
    const { go, settle } = setup('teacher', draft);
    await settle();
    const heading = document.body.appendChild(document.createElement('h2'));
    heading.id = 'preview-title';
    heading.tabIndex = -1;
    try {
      go({ preview: 'a1' });
      await settle();
      expect(document.activeElement).toBe(heading);
    } finally {
      heading.remove();
    }
  });

  it('starts focus at the new view when the control that moved there is hidden', async () => {
    vi.useFakeTimers();
    const { go, settle } = setup('teacher', draft);
    await settle();
    const list = document.body.appendChild(document.createElement('div'));
    const link = list.appendChild(document.createElement('a'));
    link.href = '#';
    const title = document.body.appendChild(document.createElement('input'));
    title.id = 'assessment-title';
    const add = document.body.appendChild(document.createElement('button'));
    add.id = 'new-assessment';
    try {
      // a link such as continue editing, whose list is hidden once the editor shows.
      link.focus();
      list.hidden = true;
      go({ edit: 'new' });
      await settle();
      expect(document.activeElement).toBe(title);
      // a control still in view keeps focus.
      add.focus();
      go();
      await settle();
      expect(document.activeElement).toBe(add);
      // the same view again moves nothing.
      add.blur();
      go();
      await settle();
      expect(document.activeElement).toBe(document.body);
    } finally {
      for (const el of [list, title, add]) el.remove();
    }
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
