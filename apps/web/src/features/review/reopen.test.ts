// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { GradingApi } from '../../api/clients';
import type { Evaluation } from '../../api/models';
import { ReopenComponent, type Reopened } from './reopen';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const settle = () => new Promise((resolve) => setTimeout(resolve));

const grade = (status: Evaluation['status']): Evaluation => ({
  id: 'e1',
  assessment_id: 'a1',
  classroom_id: 'c1',
  student_id: 's1',
  status,
  evidence: [],
  answers: {},
  late: false,
  released_at: null,
});

function setup(reopen: (id: string, reason: string) => Promise<Evaluation>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: GradingApi, useValue: { reopen } }] });
  TestBed.overrideComponent(ReopenComponent, { set: { template: '', imports: [] } });
  const component = TestBed.createComponent(ReopenComponent).componentInstance;
  component.grade = grade('finalized');
  component.classroomId = 'c1';
  component.assessmentId = '';
  const reopened: Reopened[] = [];
  component.reopened.subscribe((r) => reopened.push(r));
  return { component, reopened };
}

describe('reopening a finalized grade', () => {
  it('asks for a reason before reopening', () => {
    const reopen = vi.fn();
    const { component } = setup(reopen);
    component.askReopen();
    expect(component.reopening()).toBe(true);
    component.reopen();
    expect(reopen).not.toHaveBeenCalled();
    expect(component.reopenState.error()).toBe('Check the highlighted fields before continuing.');
  });

  it('reopens, closes and hands the draft back with the classroom and assessment asked for', async () => {
    const reopen = vi.fn(() => Promise.resolve(grade('draft')));
    const { component, reopened } = setup(reopen);
    component.askReopen();
    component.reopenForm.controls.reason.setValue('Mis-keyed');
    component.reopen();
    component.assessmentId = 'a1';
    await settle();
    expect(reopen).toHaveBeenCalledWith('e1', 'Mis-keyed');
    expect(component.reopening()).toBe(false);
    expect(component.reopenForm.dirty).toBe(false);
    expect(reopened).toEqual([{ result: grade('draft'), classroomId: 'c1', assessmentId: '' }]);
  });

  it('keeps the dialog open with the error when the reopen is refused', async () => {
    const { component, reopened } = setup(() => Promise.reject(new Error('Not allowed.')));
    component.askReopen();
    component.reopenForm.controls.reason.setValue('Fix');
    component.reopen();
    await settle();
    expect(component.reopening()).toBe(true);
    expect(component.reopenState.error()).toBe('Not allowed.');
    expect(reopened).toEqual([]);
  });
});
