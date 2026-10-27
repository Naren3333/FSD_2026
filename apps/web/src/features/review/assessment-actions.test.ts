// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { GradingApi } from '../../api/clients';
import { AssessmentActionsComponent, type AssessmentActionDone } from './assessment-actions';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const settle = () => new Promise((resolve) => setTimeout(resolve));

function setup(grading: Record<string, unknown> = {}, blocked = false) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: GradingApi, useValue: grading }] });
  TestBed.overrideComponent(AssessmentActionsComponent, { set: { template: '', imports: [] } });
  const component = TestBed.createComponent(AssessmentActionsComponent).componentInstance;
  component.classroomId = 'c1';
  component.assessmentId = 'a1';
  component.blocked = () => blocked;
  const done: AssessmentActionDone[] = [];
  const finalized = vi.fn();
  component.done.subscribe((d) => done.push(d));
  component.finalized.subscribe(finalized);
  return { component, done, finalized };
}

describe('releasing results', () => {
  it('releases, closes, then says how many with the classroom and assessment asked for', async () => {
    const release = vi.fn(() => Promise.resolve({ assessment_id: 'a1', released: 3 }));
    const { component, done } = setup({ release });
    component.askRelease();
    expect(component.releasing()).toBe(true);
    component.release();
    component.assessmentId = 'a2';
    await settle();
    expect(release).toHaveBeenCalledWith('a1');
    expect(component.releasing()).toBe(false);
    expect(done).toEqual([
      { message: '3 results released to students.', classroomId: 'c1', assessmentId: 'a1' },
    ]);
  });

  it('keeps the dialog open with the error when the release fails', async () => {
    const { component, done } = setup({
      release: () => Promise.reject(new Error('Grading is unavailable.')),
    });
    component.askRelease();
    component.release();
    await settle();
    expect(component.releasing()).toBe(true);
    expect(component.releaseState.error()).toBe('Grading is unavailable.');
    expect(done).toEqual([]);
  });

  it('does nothing without an assessment', () => {
    const release = vi.fn();
    const { component } = setup({ release });
    component.assessmentId = '';
    component.release();
    expect(release).not.toHaveBeenCalled();
  });
});

describe('finalizing every fully marked grade', () => {
  it('finalizes, says so after the page has closed what was open, and names what still needs marking', async () => {
    const finalizeAll = vi.fn(() => Promise.resolve({ assessment_id: 'a1', finalized: 1, needs_marking: 2 }));
    const { component, done, finalized } = setup({ finalizeAll });
    const order: string[] = [];
    component.finalized.subscribe(() => order.push('finalized'));
    component.done.subscribe(() => order.push('done'));
    component.askFinalizeAll();
    expect(component.bulkFinalizing()).toBe(true);
    component.finalizeAll();
    await settle();
    expect(finalizeAll).toHaveBeenCalledWith('a1');
    expect(component.bulkFinalizing()).toBe(false);
    expect(finalized).toHaveBeenCalledOnce();
    expect(order).toEqual(['finalized', 'done']);
    expect(done[0].message).toBe('1 grade finalized. 2 still need marking.');
  });

  it('refuses while the page has marks not saved yet', () => {
    const { component } = setup({}, true);
    component.askFinalizeAll();
    expect(component.bulkFinalizing()).toBe(false);
  });

  it('names the release policy and falls back to a generic title', () => {
    const { component } = setup();
    component.releasePolicy = 'manual';
    component.title = undefined;
    expect(component.releasesByHand()).toBe(true);
    expect(component.assessmentTitle()).toBe('this assessment');
    component.title = 'Fractions quiz';
    expect(component.assessmentTitle()).toBe('Fractions quiz');
  });
});
