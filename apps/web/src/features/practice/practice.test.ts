// @vitest-environment jsdom
import { WritableSignal, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ContentApi, QuestionGenerationApi } from '../../api/clients';
import type { DocumentRecord, PracticeQuestion, PracticeRequest, PracticeResult } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { PracticeComponent, isCorrect, normalizeAnswer, scoreText } from './practice';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
  // jsdom has no modal dialogs: these do what a browser's do for the parts the dialog relies on.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    if (!this.open) return;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
});

const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: 'd1',
  classroom_id: 'c1',
  title: 'Testing slides',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  sha256: 'a'.repeat(64),
  doc_type: 'teaching_material',
  ...change,
});

const source = { document_id: 'd1', chunk_id: 'k1', page: 2, text: 'A unit test checks one function.' };
const choice: PracticeQuestion = {
  prompt: 'Which test checks one function?',
  type: 'multiple_choice',
  options: ['A unit test', 'An end-to-end test'],
  answer: 'A unit test',
  accepted_answers: [],
  explanation: 'The slides say a unit test checks one function.',
  evidence: 'A unit test checks one function.',
  sources: [source],
  topic_id: null,
};
const short: PracticeQuestion = {
  ...choice,
  prompt: 'What do we call a test of one function?',
  type: 'short_answer',
  options: [],
  answer: '',
  accepted_answers: ['unit test', 'unit testing'],
};
const set = (...questions: PracticeQuestion[]): PracticeResult => ({
  questions,
  requested: questions.length,
  returned: questions.length,
  dropped: {},
  model_calls: 2,
});

/* the practice page in classroom `classroomId`, whose agent answers with `reply`. */
function render(
  reply: (request: PracticeRequest) => Promise<PracticeResult> = () => Promise.resolve(set(choice, short)),
  documents: () => Promise<DocumentRecord[]> = () =>
    Promise.resolve([doc({}), doc({ id: 'scheme', doc_type: 'marking_scheme', title: 'Old scheme' })]),
  classroomId: WritableSignal<string> = signal('c1'),
) {
  const practice = vi.fn(reply);
  const refresh = vi.fn();
  const original = vi.fn(() => Promise.resolve('blob:x'));
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: QuestionGenerationApi, useValue: { practice } },
      { provide: ContentApi, useValue: { documents, original } },
      { provide: AiUsage, useValue: { refresh } },
      {
        provide: CurriculumNames,
        useValue: {
          refresh: () => Promise.resolve([{ id: 't1', name: 'Software testing' }]),
          topicName: (id: string) => (id === 't1' ? 'Software testing' : ''),
        },
      },
      {
        provide: ClassroomContext,
        useValue: { classroomId, known: signal(true), loading: signal(false), error: signal('') },
      },
    ],
  });
  TestBed.overrideComponent(PracticeComponent, {
    set: { template: template('./practice.html'), styleUrl: undefined },
  });
  for (const [component, path] of [
    [PageHeaderComponent, '../../shared/page-header.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(PracticeComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
  const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.replace(/\s+/g, ' ').trim() === name) as
      | HTMLButtonElement
      | undefined;
  const tick = (title: string) => {
    const box = [...root.querySelectorAll('label.check')]
      .find((l) => l.textContent?.trim() === title)
      ?.querySelector('input') as HTMLInputElement;
    box.click();
    fixture.detectChanges();
  };
  const count = (type: string, value: string) => {
    const box = root.querySelector(`#practice-count-${type}`) as HTMLInputElement;
    box.value = value;
    box.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };
  const generate = async () => {
    button('Generate practice')!.click();
    fixture.detectChanges();
    await settle();
  };
  const answer = (index: number, value: string) => {
    const box = root.querySelector(`#practice-${index}-answer`) as HTMLInputElement;
    if (box.type === 'radio') {
      const option = [...root.querySelectorAll<HTMLInputElement>(`input[name="practice-${index}"]`)].find(
        (o) => o.value === value,
      )!;
      option.click();
    } else {
      box.value = value;
      box.dispatchEvent(new Event('input'));
    }
    fixture.detectChanges();
  };
  const check = async (index: number) => {
    (root.querySelector(`#practice-${index}-check`) as HTMLButtonElement).click();
    fixture.detectChanges();
    await settle();
  };
  const unsaved = () => TestBed.inject(UnsavedChanges);
  const dialog = () => root.querySelector<HTMLDialogElement>('dialog')!;
  return {
    root,
    text,
    settle,
    button,
    tick,
    count,
    generate,
    answer,
    check,
    practice,
    refresh,
    original,
    unsaved,
    dialog,
    fixture,
  };
}

describe('checking a practice answer', () => {
  it('folds case, spaces and punctuation', () => {
    expect(normalizeAnswer('  Unit-Test. ')).toBe('unit test');
    expect(normalizeAnswer('“Unit   testing”!')).toBe('unit testing');
    expect(normalizeAnswer('...')).toBe('');
  });

  it('matches a short answer against any accepted answer, and an option only exactly', () => {
    expect(isCorrect(short, 'UNIT TEST')).toBe(true);
    expect(isCorrect(short, 'unit-testing.')).toBe(true);
    expect(isCorrect(short, 'integration test')).toBe(false);
    expect(isCorrect(short, '  ')).toBe(false);
    expect(isCorrect(choice, 'A unit test')).toBe(true);
    expect(isCorrect(choice, 'An end-to-end test')).toBe(false);
  });

  it('keeps a running score of the answers checked', () => {
    const q = { question: choice, answer: '' };
    expect(
      scoreText([
        { ...q, correct: null },
        { ...q, correct: null },
      ]),
    ).toBe('2 questions to answer.');
    expect(
      scoreText([
        { ...q, correct: true },
        { ...q, correct: false },
        { ...q, correct: null },
      ]),
    ).toBe('1 of 2 correct, 1 still to answer.');
    expect(scoreText([{ ...q, correct: true }])).toBe('1 of 1 correct.');
  });
});

describe('practice', () => {
  it('offers only indexed teaching materials and says when there are none', async () => {
    const page = render();
    await page.settle();
    expect(page.text()).toContain('Testing slides');
    expect(page.text()).not.toContain('Old scheme');
    const empty = render(undefined, () => Promise.resolve([doc({ status: 'indexing' })]));
    await empty.settle();
    expect(empty.text()).toContain('Your teacher adds them under Teaching materials');
    expect(empty.button('Generate practice')).toBeUndefined();
  });

  it('shows a failure to load the materials with a way to try again', async () => {
    let fail = true;
    const page = render(undefined, () =>
      fail ? Promise.reject(new Error('Content is unavailable.')) : Promise.resolve([doc({})]),
    );
    await page.settle();
    expect(page.root.querySelector('[role="alert"]')?.textContent).toContain('Content is unavailable.');
    fail = false;
    page.button('Try again')!.click();
    await page.settle();
    expect(page.text()).toContain('Testing slides');
  });

  it('asks for a set with the choices made, and refuses one with no material', async () => {
    const page = render();
    await page.settle();
    await page.generate();
    expect(page.practice).not.toHaveBeenCalled();
    expect(page.text()).toContain('Choose at least one teaching material.');
    page.tick('Testing slides');
    page.count('short_answer', '0');
    await page.generate();
    expect(page.practice).toHaveBeenCalledWith({
      classroom_id: 'c1',
      document_ids: ['d1'],
      topic_id: null,
      counts: { multiple_choice: 3 },
      difficulty: 'introductory',
      focus: '',
      existing_prompts: [],
    });
    expect(page.refresh).toHaveBeenCalled();
    expect(page.text()).toContain('2 practice questions ready.');
    expect(page.text()).toContain('AI-written practice');
  });

  it('shows a key and its explanation only after the student checks their answer, then locks it', async () => {
    const page = render();
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    // nothing gives the answer away before a check.
    expect(page.text()).not.toContain('The slides say a unit test checks one function.');
    expect(page.text()).not.toContain('unit testing');
    expect(page.root.querySelector('.badge-good')).toBeNull();
    // checking without an answer says so and keeps the key hidden.
    await page.check(0);
    expect(page.text()).toContain('Choose an answer first.');
    expect(page.text()).not.toContain('The answer is');
    page.answer(0, 'An end-to-end test');
    await page.check(0);
    expect(page.text()).toContain('Incorrect');
    expect(page.text()).toContain('You chose: An end-to-end test. The answer is: A unit test');
    expect(page.text()).toContain('Why: The slides say a unit test checks one function.');
    expect(page.text()).toContain('Testing slides, page 2');
    expect(document.activeElement?.id).toBe('practice-0-feedback');
    expect(page.root.querySelector<HTMLInputElement>('input[name="practice-0"]')!.disabled).toBe(true);
    expect(page.root.querySelector('#practice-0-check')).toBeNull();
    // the short answer's accepted answers are still hidden.
    expect(page.text()).not.toContain('Accepted answers');
    page.answer(1, 'Unit-Test');
    await page.check(1);
    expect(page.text()).toContain('Correct');
    expect(page.text()).toContain('Accepted answers: unit test; unit testing');
    expect(page.text()).toContain('1 of 2 correct.');
  });

  it('shows the accepted answers to compare when a short answer does not match', async () => {
    const page = render(() => Promise.resolve(set(short)));
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.answer(0, 'a test of one function');
    await page.check(0);
    expect(page.text()).toContain('Compare your answer');
    expect(page.text()).toContain('Your answer: a test of one function');
    expect(page.text()).toContain('Accepted answers: unit test; unit testing');
    expect(page.text()).toContain('0 of 1 correct.');
  });

  it('opens a source at its page', async () => {
    const page = render(() => Promise.resolve(set(choice)));
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.answer(0, 'A unit test');
    await page.check(0);
    page.button('Open document Testing slides, page 2')!.click();
    await page.settle();
    expect(page.original).toHaveBeenCalledWith('d1');
    open.mockRestore();
  });

  it('asks for more questions without repeating the ones shown', async () => {
    const page = render();
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.practice.mockResolvedValueOnce(set({ ...choice, prompt: 'Which test runs fastest?' }));
    page.button('More questions')!.click();
    await page.settle();
    expect(page.practice.mock.calls[1][0].existing_prompts).toEqual([choice.prompt, short.prompt]);
    expect(page.text()).toContain('Which test runs fastest?');
    expect(page.text()).toContain('3 questions to answer.');
    expect(document.activeElement?.id).toBe('practice-2-prompt');
  });

  it('keeps an answer checked while more questions were being written', async () => {
    const page = render();
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    let deliver: (value: ReturnType<typeof set>) => void = () => undefined;
    page.practice.mockReturnValueOnce(new Promise((resolve) => (deliver = resolve)));
    page.button('More questions')!.click();
    await page.settle();
    page.answer(0, 'A unit test');
    await page.check(0);
    deliver(set({ ...choice, prompt: 'Which test runs fastest?' }));
    await page.settle();
    expect(page.text()).toContain('Which test runs fastest?');
    expect(page.text()).toContain('1 of 1 correct');
    expect(document.activeElement?.id).toBe('practice-2-prompt');
  });

  it.each([
    ['INSUFFICIENT_SOURCE', 422, 'The chosen documents say too little about this.'],
    ['GENERATION_REJECTED', 422, 'None of the practice questions passed the checks.'],
    ['AI_SPEND_LIMIT', 429, "You've used today's AI allowance. It resets at 00:00 on 23 Nov (UTC+08:00)."],
    ['AI_UNAVAILABLE', 503, 'Practice questions are not available right now. Try again later.'],
    [
      'REVIEW_UNAVAILABLE',
      503,
      'Your practice questions could not be checked this time. Try again in a moment.',
    ],
  ])('shows %s in place, keeps the choices and does not retry', async (code, status, message) => {
    const page = render(() => Promise.reject(new ApiError(message, status, code)));
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    expect(page.practice).toHaveBeenCalledTimes(1);
    const alerts = [...page.root.querySelectorAll('[role="alert"]')].map((a) => a.textContent?.trim());
    expect(alerts.some((a) => a?.includes(message))).toBe(true);
    expect(page.root.querySelector<HTMLInputElement>('label.check input')!.checked).toBe(true);
    expect(page.button('Generate practice')!.disabled).toBe(false);
    expect(page.refresh).toHaveBeenCalled();
  });

  it('waits while a set is written: the button and the classroom are locked', async () => {
    let finish: (result: PracticeResult) => void = () => undefined;
    const page = render(() => new Promise((resolve) => (finish = resolve)));
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    expect(page.text()).toContain('Writing and checking your questions');
    expect(page.button('Generate practice')!.disabled).toBe(true);
    expect(page.unsaved().locksClassroom()).toBe(true);
    finish(set(choice));
    await page.settle();
    // an unfinished set warns on unload but does not lock the classroom.
    expect(page.unsaved().locksClassroom()).toBe(false);
    expect(page.unsaved().any()).toBe(true);
  });

  it('clears the set and the choices when the classroom changes, and drops a late reply', async () => {
    const classroomId = signal('c1');
    let finish: (result: PracticeResult) => void = () => undefined;
    const page = render(() => Promise.resolve(set(choice)), undefined, classroomId);
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    expect(page.text()).toContain('Which test checks one function?');
    page.practice.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    page.button('More questions')!.click();
    classroomId.set('c2');
    page.fixture.detectChanges();
    await page.settle();
    expect(page.text()).not.toContain('Which test checks one function?');
    finish(set({ ...choice, prompt: 'A late question?' }));
    await page.settle();
    expect(page.text()).not.toContain('A late question?');
    expect(page.root.querySelector<HTMLInputElement>('label.check input')!.checked).toBe(false);
  });

  it('says how many of the questions asked for were left out, not how many drafts were refused', async () => {
    const page = render(() =>
      Promise.resolve({ ...set(choice), requested: 3, dropped: { WEAK_DISTRACTORS: 4, NOT_REVIEWED: 1 } }),
    );
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    expect(page.text()).toContain(
      '2 of the 3 questions you asked for were left out because the checks could not confirm them.',
    );
    // a set that is complete says nothing, however many drafts were rewritten to get there.
    page.practice.mockResolvedValueOnce({ ...set(choice, short), dropped: { WEAK_DISTRACTORS: 2 } });
    page.button('More questions')!.click();
    await page.settle();
    expect(page.text()).not.toContain('you asked for');
  });

  it('asks before a new set replaces answers still to check, and keeps the set on cancel', async () => {
    const page = render();
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.answer(0, 'A unit test');
    page.practice.mockResolvedValueOnce(set({ ...choice, prompt: 'Which test runs fastest?' }));
    await page.generate();
    expect(page.practice).toHaveBeenCalledTimes(1);
    expect(page.dialog().open).toBe(true);
    expect(page.dialog().textContent).toContain('Replace this practice set?');
    expect(page.dialog().textContent).toContain('2 questions in this set are not checked yet.');
    page.button('Cancel')!.click();
    page.fixture.detectChanges();
    expect(page.dialog().open).toBe(false);
    expect(page.text()).toContain('Which test checks one function?');
    expect(page.root.querySelector<HTMLInputElement>('input[name="practice-0"]')!.checked).toBe(true);
    await page.generate();
    page.button('Generate new set')!.click();
    await page.settle();
    expect(page.dialog().open).toBe(false);
    expect(page.practice).toHaveBeenCalledTimes(2);
    expect(page.text()).toContain('Which test runs fastest?');
    expect(page.text()).not.toContain('Which test checks one function?');
  });

  it('replaces a set whose answers are all checked without asking', async () => {
    const page = render(() => Promise.resolve(set(choice)));
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.answer(0, 'A unit test');
    await page.check(0);
    await page.generate();
    expect(page.dialog().open).toBe(false);
    expect(page.practice).toHaveBeenCalledTimes(2);
  });

  it('starts a new set with the choices kept', async () => {
    const page = render();
    await page.settle();
    page.tick('Testing slides');
    await page.generate();
    page.button('New set')!.click();
    page.fixture.detectChanges();
    expect(page.text()).not.toContain('Your practice');
    expect(page.root.querySelector<HTMLInputElement>('label.check input')!.checked).toBe(true);
  });
});
