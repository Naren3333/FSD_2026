// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation, Student } from '../../api/models';
import { ApiError } from '../../core/gateway';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { PagerComponent } from '../../shared/pager';
import { RosterPickerComponent } from '../../shared/roster-picker';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { ASSIGNEE_REFUSALS, assigneeRefusal } from './resit';
import { ResitStudentsComponent } from './resit-students';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

// jsdom has no modal dialogs: these do what a browser's do for the parts the dialogs rely on.
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    if (!this.open) return;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
});

const roster: Student[] = [
  { id: 's1', username: 'ana', display_name: 'Ana' },
  { id: 's2', username: 'ben', display_name: 'Ben' },
  { id: 's3', username: 'cy', display_name: 'Cy' },
];

const resit = (change: Partial<Assessment> = {}): Assessment => ({
  id: 'r1',
  classroom_id: 'c1',
  title: 'Fractions (resit)',
  status: 'published',
  due_at: null,
  late_policy: 'accept_flagged',
  questions: [],
  resit_of: 'a1',
  assignees: ['s1', 's2', 'gone'],
  ...change,
});

const graded = (student: string) => ({ student_id: student, status: 'draft' }) as Evaluation;

/* the dialog against fake apis: s1 has submitted, `gone` left the class. */
function render(assessment = resit()) {
  const assessmentApi = {
    editable: vi.fn(() => Promise.resolve(assessment)),
    setAssignees: vi.fn((_id: string, ids: string[]) => Promise.resolve({ ...assessment, assignees: ids })),
  };
  const gradingApi = {
    evaluations: vi.fn(() => Promise.resolve({ evaluations: [graded('s1')], total: 1 })),
  };
  const identityApi = {
    roster: vi.fn(() => Promise.resolve(roster)),
    rosterPage: vi.fn((_c: string, offset: number, limit: number) =>
      Promise.resolve({ students: roster.slice(offset, offset + limit), total: roster.length }),
    ),
  };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: AssessmentApi, useValue: assessmentApi },
      { provide: GradingApi, useValue: gradingApi },
      { provide: IdentityApi, useValue: identityApi },
    ],
  });
  TestBed.overrideComponent(ResitStudentsComponent, { set: { template: template('./resit-students.html') } });
  for (const [component, path] of [
    [RosterPickerComponent, '../../shared/roster-picker.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
    [StatusComponent, '../../shared/status.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [PagerComponent, '../../shared/pager.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ResitStudentsComponent);
  fixture.detectChanges();
  const dialog = fixture.componentInstance;
  const root = fixture.nativeElement as HTMLElement;
  const saved: Assessment[] = [];
  dialog.saved.subscribe((a) => saved.push(a));
  const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
  const main = () =>
    root.querySelector('dialog[aria-labelledby="resit-students-title"]') as HTMLDialogElement;
  const confirm = () =>
    root.querySelector('dialog[aria-labelledby="resit-remove-title"]') as HTMLDialogElement;
  const box = (name: string) =>
    [...root.querySelectorAll<HTMLLabelElement>('label.check')]
      .find((l) => l.querySelector('.resit-choice > span')?.textContent?.trim() === name)
      ?.querySelector('input') as HTMLInputElement;
  const tick = (name: string) => {
    const input = box(name);
    input.checked = !input.checked;
    input.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  };
  const button = (name: string, within: HTMLElement = root) =>
    [...within.querySelectorAll('button')].find(
      (b) => b.textContent?.replace(/\s+/g, ' ').trim() === name,
    ) as HTMLButtonElement;
  const open = async () => {
    dialog.open('r1', 'students-r1');
    await settle();
  };
  return {
    fixture,
    dialog,
    root,
    saved,
    settle,
    text,
    main,
    confirm,
    box,
    tick,
    button,
    open,
    assessmentApi,
  };
}

describe('changing a resit’s students', () => {
  it('loads the students, locks those who submitted and names those who left by id', async () => {
    const page = render();
    await page.open();
    expect(page.main().open).toBe(true);
    expect(page.text()).toContain('Change students: Fractions (resit)');
    expect(page.box('Ana').checked).toBe(true);
    expect(page.box('Ana').disabled).toBe(true);
    expect(page.box('Ben').checked).toBe(true);
    expect(page.box('Ben').disabled).toBe(false);
    expect(page.box('Cy').checked).toBe(false);
    expect(page.text()).toContain('Students who have submitted the resit stay on it.');
    expect(page.text()).toContain('1 student on this resit is no longer in this class and is shown by id.');
    expect(page.text()).toContain('Remove gone');
    expect(page.text()).toContain('Choose 1 to 100 students: 3 chosen.');
  });

  it('says added students see a published resit at once, and a draft is seen by no one', async () => {
    const published = render();
    await published.open();
    expect(published.text()).toContain('students you add see it at once');
    const draft = render(resit({ status: 'draft' }));
    await draft.open();
    expect(draft.text()).toContain('no student sees it until you publish it');
  });

  it('confirms taking students off a published resit, since their saved answers are deleted', async () => {
    const page = render();
    await page.open();
    page.tick('Ben');
    page.tick('Cy');
    page.button('Save students').click();
    page.fixture.detectChanges();
    expect(page.assessmentApi.setAssignees).not.toHaveBeenCalled();
    expect(page.confirm().open).toBe(true);
    expect(page.confirm().textContent).toContain('Take 1 student off this resit?');
    expect(page.confirm().textContent).toContain('Their saved answers will be deleted.');
    expect(page.confirm().textContent).toContain('1 student you added will see it at once.');
    page.button('Take off and save', page.confirm()).click();
    await page.settle();
    expect(page.assessmentApi.setAssignees).toHaveBeenCalledWith('r1', ['s1', 'gone', 's3']);
    expect(page.saved.map((a) => a.assignees)).toEqual([['s1', 'gone', 's3']]);
    expect(page.main().open).toBe(false);
    expect(page.confirm().open).toBe(false);
  });

  it('saves a draft without asking, and refuses an empty choice', async () => {
    const page = render(resit({ status: 'draft', assignees: ['s2'] }));
    page.assessmentApi.editable.mockResolvedValue(resit({ status: 'draft', assignees: ['s2'] }));
    await page.open();
    page.tick('Ben');
    page.button('Save students').click();
    page.fixture.detectChanges();
    expect(page.text()).toContain('Choose at least one student for the resit.');
    page.tick('Cy');
    page.button('Save students').click();
    await page.settle();
    expect(page.confirm().open).toBe(false);
    expect(page.assessmentApi.setAssignees).toHaveBeenCalledWith('r1', ['s3']);
  });

  it('explains each refusal and offers to reload when the list moved on', async () => {
    for (const code of ['HAS_SUBMISSIONS', 'ASSESSMENT_CHANGED', 'NOT_ASSIGNED', 'STUDENT_DELETED']) {
      const page = render(resit({ status: 'draft' }));
      await page.open();
      page.assessmentApi.setAssignees.mockRejectedValueOnce(new ApiError('raw', 409, code));
      page.tick('Cy');
      page.button('Save students').click();
      await page.settle();
      expect(page.dialog.state.error()).toBe(ASSIGNEE_REFUSALS[code]);
      expect(page.main().open).toBe(true);
      expect(page.saved).toEqual([]);
      const reload = page.button('Reload the students');
      expect(!!reload).toBe(code === 'HAS_SUBMISSIONS' || code === 'ASSESSMENT_CHANGED');
    }
    expect(assigneeRefusal(new ApiError('Assessment is unavailable.', 503))).toBe(
      'Assessment is unavailable.',
    );
  });

  it('shows a confirmed save’s refusal in the dialog, and a failed read with Try again', async () => {
    const page = render();
    await page.open();
    page.assessmentApi.setAssignees.mockRejectedValueOnce(new ApiError('raw', 409, 'HAS_SUBMISSIONS'));
    page.tick('Ben');
    page.button('Save students').click();
    page.fixture.detectChanges();
    page.button('Take off and save', page.confirm()).click();
    await page.settle();
    expect(page.confirm().open).toBe(false);
    expect(page.dialog.state.error()).toBe(ASSIGNEE_REFUSALS['HAS_SUBMISSIONS']);

    const failing = render();
    failing.assessmentApi.editable.mockRejectedValueOnce(new Error('Assessment is unavailable.'));
    await failing.open();
    expect(failing.text()).toContain('Assessment is unavailable.');
    expect(failing.button('Save students').disabled).toBe(true);
    failing.button('Try again').click();
    await failing.settle();
    expect(failing.box('Ben').checked).toBe(true);
  });

  it('refuses an assessment for the whole class, and returns focus to its opener on close', async () => {
    const page = render(resit({ assignees: undefined }));
    const opener = document.createElement('button');
    opener.id = 'students-r1';
    document.body.append(opener);
    await page.open();
    expect(page.text()).toContain(ASSIGNEE_REFUSALS['NOT_ASSIGNED']);
    page.button('Cancel').click();
    await page.settle();
    expect(page.main().open).toBe(false);
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });
});
