// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { Section } from '../../shared/section';
import { StatusComponent } from '../../shared/status';
import { AnswerSheetComponent } from './answer-sheet';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
  // jsdom has no scrolling.
  vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined);
});
afterEach(() => vi.useRealTimers());

const quiz: Assessment = {
  id: 'a1',
  classroom_id: 'c1',
  title: 'Fractions',
  questions: [
    { id: 'q1', prompt: 'Half of 1?', type: 'numerical', options: null, points: 1, topic_id: 's1' },
  ],
  due_at: '2026-10-15T00:00:00Z',
  late_policy: 'accept_flagged',
};
const submitted: AssessmentSummary = {
  id: 'a1',
  title: 'Fractions',
  submitted: true,
  status: 'published',
  opens_at: null,
  due_at: quiz.due_at,
  late_policy: 'accept_flagged',
  question_count: 1,
  points: 1,
};
const choice: Assessment = {
  id: 'a2',
  classroom_id: 'c1',
  title: 'Shapes',
  questions: [
    {
      id: 'q1',
      prompt: 'How many sides has a triangle?',
      type: 'multiple_choice',
      options: ['Two', 'Three', 'Four'],
      points: 2,
      topic_id: 's1',
    },
  ],
  due_at: null,
  late_policy: 'accept_flagged',
};
const open: AssessmentSummary = {
  ...submitted,
  id: 'a2',
  title: 'Shapes',
  submitted: false,
  due_at: null,
  points: 2,
};

/* a student's answer sheet, with its template, for the assessment they have submitted or (`answering`) the multiple choice one they have not. */
function render(answering = false) {
  const api = {
    get: () => Promise.resolve(answering ? choice : quiz),
    saved: () => Promise.resolve({ answers: {}, saved_at: null }),
    save: vi.fn((_id: string, answers: Record<string, string>) =>
      Promise.resolve({ answers, saved_at: '2026-10-17T02:00:00Z' }),
    ),
    submission: () =>
      Promise.resolve({ answers: { q1: '0.5' }, submitted_at: '2026-10-16T01:00:00Z', late: true }),
    submit: vi.fn(() => Promise.resolve({ late: false })),
  };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: AssessmentApi, useValue: api },
      { provide: Session, useValue: { isStudent: signal(true), isTeacher: signal(false) } },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
    ],
  });
  TestBed.overrideComponent(AnswerSheetComponent, { set: { template: template('./answer-sheet.html') } });
  for (const [component, path] of [
    [FieldComponent, '../../shared/field.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
    [StatusComponent, '../../shared/status.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(AnswerSheetComponent);
  const assessments = new Section<AssessmentSummary[]>();
  assessments.set([answering ? open : submitted]);
  const state = new ActionState();
  fixture.componentRef.setInput('assessments', assessments);
  fixture.componentRef.setInput('state', state);
  fixture.detectChanges();
  const settle = async () => {
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();
  };
  const root = fixture.nativeElement as HTMLElement;
  const submit = () => root.querySelector<HTMLButtonElement>('button[type="submit"]');
  return { sheet: fixture.componentInstance, root, settle, api, state, submit };
}

describe('the answer sheet of a submitted assessment', () => {
  it('shows the student’s answers read only, with when they submitted it and that it was late', async () => {
    vi.useFakeTimers();
    const { sheet, root, settle, submit } = render();
    void sheet.open('a1');
    await settle();
    expect(root.querySelector('#answer-q1')).toBeNull();
    expect(root.querySelector('.answer-text')?.textContent?.trim()).toBe('0.5');
    expect(root.textContent).toContain('Submitted on ');
    expect(root.textContent).toContain(', after the due date. These are your submitted answers');
    expect(root.textContent).not.toContain('Clear form');
    expect(submit()?.textContent?.trim()).toBe('Submitted');
  });

  it('opens on its own view, with a way back to the list and its title focused', async () => {
    vi.useFakeTimers();
    const { sheet, root, settle } = render();
    void sheet.open('a1');
    await settle();
    await settle();
    const back = root.querySelector('a.back-link');
    expect(back?.getAttribute('href')).toBe('/assessments');
    expect(back?.textContent?.replace(/\s+/g, ' ').trim()).toBe('Back to Assessments');
    const title = root.querySelector('#assessment-view-title');
    expect(title?.textContent?.trim()).toBe('Fractions');
    expect(document.activeElement).toBe(title);
    expect(window.scrollTo).toHaveBeenCalledWith({ top: 0 });
    expect(root.textContent).toContain('1 question · 1 mark');
  });
});

describe('a multiple choice answer', () => {
  it('is a radio group under one name, whose choice is saved once it rests', async () => {
    vi.useFakeTimers();
    const { sheet, root, settle, api } = render(true);
    void sheet.open('a2');
    await settle();
    const radios = Array.from(root.querySelectorAll<HTMLInputElement>('input[type="radio"]'));
    expect(radios.map((r) => r.closest('label')?.textContent?.trim())).toEqual(['Two', 'Three', 'Four']);
    expect(new Set(radios.map((r) => r.name))).toEqual(new Set(['answer-q1']));
    expect(root.querySelector('.choice legend')?.textContent?.trim()).toBe('How many sides has a triangle?');
    expect(root.textContent).toContain('0 of 1 answered');
    radios[1].click();
    await settle();
    expect(sheet.answers.controls['q1'].value).toBe('Three');
    expect(root.textContent).toContain('1 of 1 answered');
    await vi.advanceTimersByTimeAsync(1500);
    expect(api.save).toHaveBeenCalledExactlyOnceWith('a2', { q1: 'Three' });
  });

  it('says to choose an answer when it is submitted without one', async () => {
    vi.useFakeTimers();
    const { sheet, root, settle, state } = render(true);
    void sheet.open('a2');
    await settle();
    sheet.submit();
    await settle();
    const group = root.querySelector('fieldset.choice');
    expect(group?.getAttribute('aria-invalid')).toBe('true');
    expect(group?.getAttribute('aria-describedby')).toBe('answer-q1-error');
    expect(root.querySelector('#answer-q1-error')?.textContent?.trim()).toBe('Choose an answer.');
    expect(state.error()).toBe('Check the highlighted fields before continuing.');
    // the page's status shows in the bar beside submit.
    expect(root.querySelector('.answer-bar [role="alert"]')?.textContent).toContain(
      'Check the highlighted fields',
    );
    expect(document.activeElement).toBe(root.querySelector('input[type="radio"]'));
  });

  it('moves focus to the title once submitted, since submit is then disabled', async () => {
    vi.useFakeTimers();
    const { sheet, root, settle, api, submit } = render(true);
    void sheet.open('a2');
    await settle();
    const radio = root.querySelectorAll<HTMLInputElement>('input[type="radio"]')[1];
    radio.click();
    submit()?.focus();
    sheet.submit();
    await settle();
    await settle();
    expect(api.submit).toHaveBeenCalledExactlyOnceWith('a2', { q1: 'Three' });
    expect(submit()?.disabled).toBe(true);
    expect(document.activeElement).toBe(root.querySelector('#assessment-view-title'));
  });
});
