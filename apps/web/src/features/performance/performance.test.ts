// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { readFileSync } from 'node:fs';
import { of } from 'rxjs';
import { beforeAll, describe, expect, it } from 'vitest';
import { AnalyticsApi, IdentityApi } from '../../api/clients';
import type { Performance, Student } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { FieldComponent } from '../../shared/field';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { PerformanceComponent } from './performance';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const ana: Student = { id: 's1', username: 'ana', display_name: 'Ana Lim' };
const evidence = (topics: Performance['topics']): Performance => ({
  student_id: 's1',
  classroom_id: 'c1',
  evidence_status: topics.length ? 'available' : 'no_finalized_evidence',
  interpretation: '',
  topics,
});

/** A teacher's performance page for classroom c1, opened from a link to Ana's evidence, whose roster and evidence
 * load as given. */
function render(roster: Promise<Student[]>, performance: Promise<Performance>, student = 's1') {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: Session,
        useValue: { profile: signal({ id: 't1' }), isTeacher: signal(true), isStudent: signal(false) },
      },
      { provide: IdentityApi, useValue: { roster: () => roster } },
      { provide: AnalyticsApi, useValue: { performance: () => performance } },
      {
        provide: CurriculumNames,
        useValue: { need: () => Promise.resolve(), topicName: (id: string) => (id === 'k1' ? 'Halves' : '') },
      },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
      { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({ student })) } },
    ],
  });
  TestBed.overrideComponent(PerformanceComponent, { set: { template: template('./performance.html') } });
  for (const [component, path] of [
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(PerformanceComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent ?? '';
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  return { text, settle };
}

describe('performance states', () => {
  it('shows the roster loading in the student field', async () => {
    const page = render(new Promise(() => undefined), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('Loading students…');
  });

  it('says when the classroom has no students to choose', async () => {
    const page = render(Promise.resolve([]), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('No students are enrolled in this classroom yet.');
  });

  it('shows the roster’s failure', async () => {
    const page = render(Promise.reject(new Error('Identity is unavailable.')), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('Identity is unavailable.');
  });

  it('shows the linked student’s evidence loading, then their topics', async () => {
    let resolve!: (p: Performance) => void;
    const page = render(Promise.resolve([ana]), new Promise((yes) => (resolve = yes)));
    await page.settle();
    expect(page.text()).toContain('Loading performance…');
    resolve(
      evidence([
        {
          topic_id: 'k1',
          attempted: 6,
          correct: 2,
          observed_accuracy: 2 / 6,
          evidence_status: 'observed',
          weakness_indicator: true,
        },
      ]),
    );
    await page.settle();
    expect(page.text()).not.toContain('Loading performance…');
    expect(page.text()).toContain('Halves');
    expect(page.text()).toContain('Needs practice');
  });

  it('explains a student with no finalized evidence', async () => {
    const page = render(Promise.resolve([ana]), Promise.resolve(evidence([])));
    await page.settle();
    expect(page.text()).toContain('No finalized assessment evidence for this student yet.');
  });

  it('shows the failure, not the empty text, when the evidence fails', async () => {
    const page = render(Promise.resolve([ana]), Promise.reject(new Error('Analytics is unavailable.')));
    await page.settle();
    expect(page.text()).toContain('Analytics is unavailable.');
    expect(page.text()).not.toContain('No finalized assessment evidence');
  });
});
