// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { PerformanceAgentApi } from '../../api/clients';
import type { ClassInsights, ClassInsightsRequest } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { FieldComponent } from '../../shared/field';
import { SectionStateComponent } from '../../shared/section-state';
import { ClassInsightsComponent, classEvidenceText } from './class-insights';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const answer: ClassInsights = {
  classroom_id: 'c1',
  question: 'Who is struggling, and with which topics?',
  struggling: [{ topic_id: 'k1', student_ids: ['s1', 's2'], text: 'Both lost most marks on halves.' }],
  observations: [
    {
      text: 'Halving fractions is the weakest topic.',
      topic_ids: ['k1'],
      student_ids: ['s1'],
      evidence_ids: ['E1'],
    },
  ],
  possible_misconceptions: [{ text: 'Adds the denominators.', evidence_ids: ['E1'] }],
  reteach: [{ topic_id: 'k1', text: 'Model halving with fraction strips.', evidence_ids: ['E1'] }],
  students: [
    { student_id: 's1', display_name: 'Ana Lim' },
    { student_id: 's2', display_name: 'Ben Tan' },
  ],
  evidence: [
    {
      evidence_id: 'E1',
      student_id: 's1',
      question_id: 'q1',
      evaluation_id: 'e1',
      assessment_id: 'a1',
      assessment_title: 'Fractions quiz',
      topic_id: 'k1',
      prompt: 'What is half of 3/4?',
      awarded: 0,
      possible: 2,
    },
  ],
  steps: [
    { tool: 'topic_questions', reason: 'Halves look weak.', target: 'Halves', outcome: 'ok' },
    { tool: 'read_answers', reason: 'See the lost marks.', target: 'Q1', outcome: 'refused' },
  ],
  model_turns: 3,
  dropped: { NOT_SUPPORTED: 2 },
  scope: { population: 250, compared: 200, partial: true, selection: 'roster_order' },
  requires_teacher_review: true,
};

/* the class-insights panel for classroom `classroomId`, whose agent answers with `reply`. */
function render(
  reply: (request: ClassInsightsRequest) => Promise<ClassInsights> = () => Promise.resolve(answer),
  classroomId = signal('c1'),
) {
  const classInsights = vi.fn(reply);
  const refresh = vi.fn();
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: PerformanceAgentApi, useValue: { classInsights } },
      { provide: AiUsage, useValue: { refresh } },
      {
        provide: CurriculumNames,
        useValue: { need: () => Promise.resolve(), topicName: (id: string) => (id === 'k1' ? 'Halves' : '') },
      },
      { provide: ClassroomContext, useValue: { classroomId } },
    ],
  });
  for (const [component, path] of [
    [ClassInsightsComponent, './class-insights.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(ClassInsightsComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const text = () => root.textContent?.replace(/\s+/g, ' ') ?? '';
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const field = () => root.querySelector('#class-insights-question') as HTMLTextAreaElement;
  const type = (value: string) => {
    field().value = value;
    field().dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as
      | HTMLButtonElement
      | undefined;
  const ask = async (question?: string) => {
    if (question !== undefined) type(question);
    (root.querySelector('#class-insights-ask') as HTMLButtonElement).click();
    fixture.detectChanges();
    await settle();
  };
  const alerts = () => [...root.querySelectorAll('[role="alert"]')].map((a) => a.textContent?.trim());
  return { root, text, settle, field, type, button, ask, alerts, classInsights, refresh, fixture };
}

describe('class insights', () => {
  it('shows each part of the answer with its students, topics and cited answers', async () => {
    const page = render();
    expect(page.text()).toContain('AI draft for your review');
    expect(page.text()).toContain("can't look a student up by name");
    // the teacher types in the field, so focus leaves it when the answer arrives.
    page.field().focus();
    await page.ask('Who is struggling, and with which topics?');
    expect(page.classInsights).toHaveBeenCalledWith({
      classroom_id: 'c1',
      question: 'Who is struggling, and with which topics?',
      previous_questions: [],
    });
    const text = page.text();
    expect(text).toContain('You asked: Who is struggling, and with which topics?');
    expect(text).toContain('Covers the first 200 of 250 students in roster order.');
    for (const heading of ['Struggling', 'Observations', 'Possible misconceptions', 'What to reteach'])
      expect([...page.root.querySelectorAll('h4')].map((h) => h.textContent?.trim())).toContain(heading);
    expect(text).toContain('Both lost most marks on halves.');
    expect(text).toContain('Topics: Halves');
    expect(text).toContain('Ana Lim · Fractions quiz · What is half of 3/4? · 0 of 2 marks');
    expect(text).toContain("2 statements were left out because they did not rest on this class's results.");
    expect(text).toContain('How the agent worked (2 steps, 3 model turns)');
    expect(text).toContain('Read the topic’s questions: Halves (done). Why: Halves look weak.');
    expect(text).toContain('Read answers: Q1 (not allowed).');
    const links = [...page.root.querySelectorAll<HTMLAnchorElement>('.student-links a')];
    expect(links.map((a) => a.textContent?.trim())).toEqual(['Ana Lim', 'Ben Tan', 'Ana Lim']);
    expect(links[1].getAttribute('href')).toBe('/performance?student=s2');
    // the question moves into the answer, and the field is ready for a follow-up, not marked as wrong.
    expect(page.field().value).toBe('');
    expect(page.field().classList.contains('ng-touched')).toBe(false);
    expect(page.button('Ask a follow-up')).toBeDefined();
    expect(page.refresh).toHaveBeenCalledTimes(1);
  });

  it('cuts a long prompt in a cited answer', () => {
    const line = classEvidenceText({ ...answer.evidence[0], prompt: 'x'.repeat(200) }, 'Ana Lim');
    expect(line).toBe(`Ana Lim · Fractions quiz · ${'x'.repeat(119)}… · 0 of 2 marks`);
  });

  it('fills the question from a suggestion, ready to edit or ask', async () => {
    const page = render();
    page.button('What should I reteach first?')?.click();
    page.fixture.detectChanges();
    expect(page.field().value).toBe('What should I reteach first?');
    await page.ask();
    expect(page.classInsights.mock.calls[0][0].question).toBe('What should I reteach first?');
  });

  it('asks with Ctrl+Enter, and not with an empty question', async () => {
    const page = render();
    await page.ask('   ');
    expect(page.classInsights).not.toHaveBeenCalled();
    expect(page.text()).toContain('Enter a question of 1 to 500 characters.');
    page.type('Who needs help?');
    page.field().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true }));
    await page.settle();
    expect(page.classInsights).toHaveBeenCalledTimes(1);
  });

  it('sends up to three earlier questions with a follow-up, and none after New question', async () => {
    const page = render(async (request) => ({ ...answer, question: request.question }));
    for (const question of ['One?', 'Two?', 'Three?', 'Four?']) await page.ask(question);
    expect(page.classInsights.mock.calls.map((call) => call[0].previous_questions)).toEqual([
      [],
      ['One?'],
      ['One?', 'Two?'],
      ['One?', 'Two?', 'Three?'],
    ]);
    expect([...page.root.querySelectorAll('.earlier-questions li')].map((li) => li.textContent)).toEqual([
      'Two?',
      'Three?',
      'Four?',
    ]);
    page.button('New question')?.click();
    page.fixture.detectChanges();
    expect(page.text()).not.toContain('You asked:');
    expect(page.text()).not.toContain('Following up on');
    await page.ask('Fresh?');
    expect(page.classInsights.mock.calls.at(-1)?.[0].previous_questions).toEqual([]);
  });

  it('clears the answer and earlier questions when the classroom changes', async () => {
    const classroomId = signal('c1');
    const page = render(undefined, classroomId);
    await page.ask('Who is struggling?');
    expect(page.text()).toContain('You asked:');
    classroomId.set('c2');
    page.fixture.detectChanges();
    await page.settle();
    expect(page.text()).not.toContain('You asked:');
    await page.ask('And here?');
    expect(page.classInsights.mock.calls.at(-1)?.[0]).toEqual({
      classroom_id: 'c2',
      question: 'And here?',
      previous_questions: [],
    });
  });

  it('locks the classroom while the agent works', async () => {
    let finish!: (value: ClassInsights) => void;
    const page = render(() => new Promise((resolve) => (finish = resolve)));
    const unsaved = TestBed.inject(UnsavedChanges);
    await page.ask('Who is struggling?');
    expect(page.text()).toContain("Reading the class's results and drafting an answer…");
    expect(unsaved.locksClassroom()).toBe(true);
    expect(page.button('Ask')?.disabled).toBe(true);
    finish(answer);
    await page.settle();
    expect(unsaved.locksClassroom()).toBe(false);
  });

  it('says when the class has no released results, as an empty state', async () => {
    const page = render(() =>
      Promise.reject(
        new ApiError(
          'This classroom has no released results yet, so there is nothing to answer from.',
          422,
          'NO_EVIDENCE',
        ),
      ),
    );
    await page.ask('Who is struggling?');
    expect(page.text()).toContain('This classroom has no released results yet');
    expect(page.root.querySelector('.empty')).not.toBeNull();
    expect(page.alerts()).toEqual([]);
    expect(page.field().value).toBe('Who is struggling?');
    expect(page.refresh).toHaveBeenCalledTimes(1);
  });

  it('says when nothing in the class’s results answers the question', async () => {
    const page = render(() =>
      Promise.reject(
        new ApiError(
          "Nothing in this class's released results answers this question yet. Try asking about a topic or the class as a whole.",
          422,
          'NOTHING_TO_ANSWER',
        ),
      ),
    );
    await page.ask('Who is struggling?');
    expect(page.text()).toContain('Nothing in this class');
    expect(page.root.querySelector('.empty')).not.toBeNull();
    expect(page.alerts()).toEqual([]);
    expect(page.field().value).toBe('Who is struggling?');
  });

  it('says when no part of the answer rested on the class’s evidence, without a dropped note', async () => {
    const page = render(() =>
      Promise.reject(
        new ApiError(
          "The drafted answer did not rest on this class's evidence, so it was not shown. Try again.",
          422,
          'INSIGHTS_REJECTED',
        ),
      ),
    );
    await page.ask('Who is struggling?');
    expect(page.text()).toContain('so it was not shown. Try again.');
    expect(page.text()).not.toContain('left out');
    expect(page.alerts()).toEqual([]);
    expect(page.field().value).toBe('Who is struggling?');
  });

  it('shows why it failed, keeps the question and does not retry', async () => {
    const page = render(() =>
      Promise.reject(
        new ApiError("You've used today's AI allowance. It resets at 12:00 am.", 429, 'AI_SPEND_LIMIT'),
      ),
    );
    await page.ask('Who is struggling?');
    expect(page.alerts()).toEqual(["You've used today's AI allowance. It resets at 12:00 am."]);
    expect(page.field().value).toBe('Who is struggling?');
    expect(page.classInsights).toHaveBeenCalledTimes(1);
    expect(page.refresh).toHaveBeenCalledTimes(1);
    expect(page.button('Ask')).toBeDefined();
  });

  it('keeps the last answer on screen when a follow-up fails', async () => {
    let fail = false;
    const page = render(async (request) => {
      if (fail) throw new ApiError('The agent is busy. Try again in a minute.', 429, 'AI_BUSY');
      return { ...answer, question: request.question };
    });
    await page.ask('Who is struggling?');
    fail = true;
    await page.ask('And with recursion?');
    expect(page.alerts()).toEqual(['The agent is busy. Try again in a minute.']);
    expect(page.text()).toContain('You asked: Who is struggling?');
    expect(page.field().value).toBe('And with recursion?');
  });
});
