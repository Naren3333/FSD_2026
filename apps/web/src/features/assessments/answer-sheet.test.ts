// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
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

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

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

/* a student's answer sheet, with its template, for an assessment they have submitted. */
function render() {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: AssessmentApi,
        useValue: {
          get: () => Promise.resolve(quiz),
          submission: () =>
            Promise.resolve({ answers: { q1: '0.5' }, submitted_at: '2026-10-16T01:00:00Z', late: true }),
        },
      },
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
  assessments.set([submitted]);
  fixture.componentRef.setInput('assessments', assessments);
  fixture.componentRef.setInput('state', new ActionState());
  fixture.detectChanges();
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { fixture, sheet: fixture.componentInstance, root: fixture.nativeElement as HTMLElement, settle };
}

describe('the answer sheet of a submitted assessment', () => {
  it('shows the student’s answers read only, with when they submitted it and that it was late', async () => {
    const { sheet, root, settle } = render();
    sheet.open('a1');
    await settle();
    const answer = root.querySelector('#answer-q1') as HTMLInputElement;
    expect(answer.value).toBe('0.5');
    expect(answer.disabled).toBe(true);
    expect(root.textContent).toContain('Submitted on ');
    expect(root.textContent).toContain(', after the due date. These are your submitted answers');
    expect(root.textContent).not.toContain('Clear form');
    expect(root.querySelector('button')?.textContent?.trim()).toBe('Submitted');
  });
});
