// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { AssessmentApi, GradingApi } from '../../api/clients';
import type { Evaluation } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { SectionStateComponent } from '../../shared/section-state';
import { ResultsComponent } from './results';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
});

/** The results page for a classroom whose results load as `results` says. */
async function render(results: Promise<Evaluation[]>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: GradingApi, useValue: { mine: () => results } },
      { provide: AssessmentApi, useValue: { get: () => Promise.reject(new Error('unused')) } },
      { provide: CurriculumNames, useValue: { need: () => Promise.resolve(), topicName: () => '' } },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
    ],
  });
  TestBed.overrideComponent(ResultsComponent, { set: { template: template('./results.html') } });
  TestBed.overrideComponent(SectionStateComponent, {
    set: { template: template('../../shared/section-state.html') },
  });
  const fixture = TestBed.createComponent(ResultsComponent);
  fixture.detectChanges();
  const text = () => (fixture.nativeElement as HTMLElement).textContent ?? '';
  /** Lets the page's pending reads finish, then renders what they left. */
  const settle = async () => {
    await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { text, settle };
}

describe('results list states', () => {
  it('shows loading, not the empty text, until the list arrives, then the empty text', async () => {
    let resolve!: (results: Evaluation[]) => void;
    const page = await render(new Promise((yes) => (resolve = yes)));
    expect(page.text()).toContain('Loading results…');
    expect(page.text()).not.toContain('No finalized results yet');
    resolve([]);
    await page.settle();
    expect(page.text()).not.toContain('Loading results…');
    expect(page.text()).toContain('No finalized results yet');
  });

  it('shows the failure, not the empty text, when the list fails', async () => {
    const page = await render(Promise.reject(new Error('Grading is unavailable.')));
    await page.settle();
    expect(page.text()).toContain('Grading is unavailable.');
    expect(page.text()).not.toContain('No finalized results yet');
  });
});
