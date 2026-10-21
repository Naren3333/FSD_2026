// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { of } from 'rxjs';
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
    get: vi.fn(() => Promise.resolve(open)),
    saved: vi.fn(() => Promise.resolve({ answers: saved, saved_at: null })),
    save: vi.fn((_id: string, answers: Record<string, string>) =>
      Promise.resolve({ answers, saved_at: '2026-10-17T02:00:00Z' }),
    ),
    submit: vi.fn(() => Promise.resolve({ id: 's1', status: 'submitted', late: false })),
    submission: vi.fn(() =>
      Promise.resolve({ answers: { [QUESTION]: '0.5' }, submitted_at: '2026-10-16T01:00:00Z', late: true }),
    ),
    update: vi.fn((_id: string, changed: unknown) => Promise.resolve(changed)),
    details: vi.fn((_id: string, changed: unknown) => Promise.resolve(changed)),
    remove: vi.fn((id: string) => Promise.resolve({ id })),
  };
  const classroomId = signal('c1');
  const archived = signal<Topic[]>([]);
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
      { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({})) } },
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
  const settle = () => vi.advanceTimersByTimeAsync(0);
  return {
    page: fixture.componentInstance,
    sheet: sheetFixture.componentInstance,
    api,
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

describe("a teacher's preview", () => {
  it('closes from the same button that opened it', async () => {
    vi.useFakeTimers();
    const { sheet, api, settle } = setup('teacher');
    await settle();
    sheet.togglePreview('a1');
    await settle();
    expect(sheet.selected()?.id).toBe('a1');
    sheet.togglePreview('a1');
    expect(sheet.selected()).toBeNull();
    expect(api.get).toHaveBeenCalledTimes(1);
  });

  it('returns focus to the button that opened it when its own Close preview closes it', async () => {
    vi.useFakeTimers();
    const { sheet, settle } = setup('teacher');
    await settle();
    const toggle = document.body.appendChild(document.createElement('button'));
    toggle.id = 'preview-a1';
    try {
      sheet.togglePreview('a1');
      await settle();
      sheet.closePreview(true);
      await settle();
      expect(sheet.selected()).toBeNull();
      expect(document.activeElement).toBe(toggle);
    } finally {
      toggle.remove();
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
