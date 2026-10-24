// @vitest-environment jsdom
import { SimpleChange, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { QuestionGenerationApi } from '../../api/clients';
import type { DocumentRecord, GenerateResult, Question } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { CurriculumNames } from '../../core/curriculum-names';
import { Section } from '../../shared/section';
import {
  QuestionGeneratorComponent,
  droppedSummary,
  reviewNote,
  roundSummary,
  sourceDocuments,
} from './generator';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: 'd1',
  classroom_id: 'c1',
  title: 'Testing slides',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  doc_type: 'teaching_material',
  ...change,
});
const draft = (prompt: string): GenerateResult['questions'][number] => ({
  question: {
    prompt,
    type: 'multiple_choice',
    options: ['A unit test', 'An end-to-end test'],
    answer: 'A unit test',
    points: 1,
    topic_id: 's1',
    sources: [{ document_id: 'd1', chunk_id: 'k1', page: 2, text: 'A unit test checks one function.' }],
    origin: 'generated',
  },
  evidence: 'A unit test checks one function.',
  explanation: 'The slides define a unit test as checking one function.',
});
const result = (change: Partial<GenerateResult> = {}): GenerateResult => ({
  questions: [draft('Which test checks one function?'), draft('Which test is fastest?')],
  requested: 3,
  requested_by_type: { multiple_choice: 3 },
  returned: 2,
  dropped: { EVIDENCE_NOT_FOUND: 1 },
  requires_teacher_review: true,
  ...change,
});

function setup() {
  const generate = vi.fn(() => Promise.resolve(result()));
  const refresh = vi.fn();
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: QuestionGenerationApi, useValue: { generate } },
      { provide: AiUsage, useValue: { refresh } },
      { provide: CurriculumNames, useValue: { topics: signal([]) } },
    ],
  });
  TestBed.overrideComponent(QuestionGeneratorComponent, { set: { template: '', imports: [] } });
  const generator = TestBed.createComponent(QuestionGeneratorComponent).componentInstance;
  const documents = new Section<DocumentRecord[]>();
  documents.set([
    doc({}),
    doc({ id: 'scheme', doc_type: 'marking_scheme' }),
    doc({ id: 'new', status: 'indexing' }),
  ]);
  generator.documents = documents;
  generator.classroomId = 'c1';
  const kept: Omit<Question, 'id'>[] = [];
  generator.keep.subscribe((q) => kept.push(q));
  return { generator, generate, refresh, kept };
}

describe('generation sources', () => {
  it('offers only indexed teaching materials', () => {
    const docs = [
      doc({}),
      doc({ id: 'scheme', doc_type: 'marking_scheme' }),
      doc({ id: 'x', status: 'failed' }),
    ];
    expect(sourceDocuments(docs).map((d) => d.id)).toEqual(['d1']);
  });

  it('says why drafts were left out', () => {
    expect(droppedSummary({})).toBe('');
    expect(droppedSummary({ EVIDENCE_NOT_FOUND: 1, DUPLICATE: 2 })).toBe(
      '3 drafts were left out: 1 because its supporting quote was not in your materials; 2 because it repeated another question.',
    );
    expect(droppedSummary({ NEEDS_MATERIALS: 1 })).toBe(
      '1 draft was left out: 1 because it only made sense with your materials open.',
    );
    expect(droppedSummary({ LONG_KEY: 1 })).toBe(
      '1 draft was left out: 1 because its answer was much longer than the other options.',
    );
    expect(droppedSummary({ WRONG_KEY: 1, WEAK_DISTRACTORS: 1 })).toBe(
      '2 drafts were left out: 1 because the reviewer worked out a different answer; 1 because its wrong options were too obvious.',
    );
  });

  it('describes each round of checking', () => {
    const round = {
      generated: 3,
      passed_checks: 3,
      passed_review: 3,
      rejected: {},
      reviewed: true,
      retried: false,
    };
    expect(roundSummary(round, 0)).toBe('Round 1: 3 written, 3 passed the checks, 3 passed the reviewer.');
    expect(
      roundSummary(
        {
          ...round,
          passed_review: 2,
          rejected: { WRONG_KEY: 1 },
          sent_back: { WRONG_KEY: 1 },
          retried: true,
        },
        1,
      ),
    ).toBe(
      'Round 2: 3 written, 3 passed the checks, 2 passed the reviewer; 1 sent back to improve because the reviewer worked out a different answer.',
    );
    expect(roundSummary({ ...round, passed_review: 2, rejected: { WRONG_KEY: 1 } }, 2)).toBe(
      'Round 3: 3 written, 3 passed the checks, 2 passed the reviewer; 1 left out because the reviewer worked out a different answer.',
    );
    expect(roundSummary({ ...round, reviewed: false }, 0)).toBe(
      'Round 1: 3 written, 3 passed the checks, not reviewed.',
    );
    expect(roundSummary({ ...round, passed_review: 2, rejected: { NOT_REVIEWED: 1 } }, 0)).toContain(
      '; 1 got no verdict from the reviewer and was kept.',
    );
    expect(roundSummary({ ...round, passed_review: 2, rejected: { WEAK_DISTRACTORS: 1 } }, 2)).toContain(
      '; 1 flagged because its wrong options were too obvious.',
    );
    expect(
      roundSummary(
        {
          ...round,
          passed_review: 2,
          rejected: { WEAK_DISTRACTORS: 1 },
          sent_back: { WEAK_DISTRACTORS: 1 },
          retried: true,
        },
        0,
      ),
    ).toContain('; 1 sent back to improve because its wrong options were too obvious.');
    expect(
      roundSummary(
        {
          ...round,
          passed_review: 1,
          rejected: { DUPLICATE: 1, WRONG_KEY: 1 },
          sent_back: { WRONG_KEY: 1 },
          retried: true,
        },
        0,
      ),
    ).toContain(
      '; 1 left out because it repeated another question; 1 sent back to improve because the reviewer worked out a different answer.',
    );
    expect(
      roundSummary(
        { ...round, generated: 0, passed_checks: 0, passed_review: 0, rejected: { UNREADABLE_REPLY: 1 } },
        1,
      ),
    ).toContain("1 left out because the model's reply could not be read");
    expect(
      roundSummary(
        {
          ...round,
          passed_review: 1,
          rejected: { AMBIGUOUS: 1, NOT_ANSWERABLE: 1 },
          sent_back: { AMBIGUOUS: 1, NOT_ANSWERABLE: 1 },
          retried: true,
        },
        0,
      ),
    ).toContain(
      '; 1 sent back to improve because more than one answer could be right; 1 sent back to improve because your materials did not contain its answer.',
    );
  });

  it('words the reviewer status of a draft', () => {
    expect(reviewNote({ status: 'passed', note: '', round: 1 })).toBe('');
    expect(reviewNote({ status: 'flagged', note: 'Option C is obviously wrong.', round: 1 })).toBe(
      'Reviewer: Option C is obviously wrong.',
    );
    expect(
      reviewNote({
        status: 'flagged',
        note: 'The answer is much longer than the other options.',
        source: 'check',
      }),
    ).toBe('Check: The answer is much longer than the other options.');
    expect(reviewNote({ status: 'not_reviewed', note: 'The reviewer failed.' })).toBe(
      'Not reviewed: The reviewer failed.',
    );
    expect(reviewNote({ status: 'not_reviewed', note: '' })).toBe('');
    expect(reviewNote(undefined)).toBe('');
  });
});

describe('the question generator', () => {
  it('asks for what it needs before generating', () => {
    const { generator, generate } = setup();
    generator.generate();
    expect(generator.note()).toBe('Choose at least one teaching material.');
    generator.toggleDocument('d1', true);
    generator.generate();
    expect(generator.note()).toBe('Choose the topic the questions assess.');
    generator.topic.setValue('s1');
    generator.toggleType('multiple_choice', false);
    generator.toggleType('short_answer', false);
    generator.generate();
    expect(generator.note()).toBe('Choose at least one question type.');
    generator.toggleType('free_response', true);
    generator.setCount('free_response', 11);
    generator.generate();
    expect(generator.note()).toBe('Ask for 1–10 questions of each type you tick.');
    generator.setCount('free_response', 0);
    generator.generate();
    expect(generator.note()).toBe('Ask for 1–10 questions of each type you tick.');
    expect(generate).not.toHaveBeenCalled();
  });

  it('starts with three multiple choice and two short answer, five in all', () => {
    const { generator } = setup();
    expect(generator.chosenTypes()).toEqual(['multiple_choice', 'short_answer']);
    expect(generator.counts()).toEqual({ multiple_choice: 3, short_answer: 2, free_response: 2 });
    expect(generator.totalText()).toBe('5 questions in total (up to 10).');
    generator.setCount('multiple_choice', 1);
    generator.toggleType('short_answer', false);
    expect(generator.totalText()).toBe('1 question in total (up to 10).');
  });

  it('leaves an unticked type out of the request and the total', async () => {
    const { generator, generate } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.toggleType('multiple_choice', false);
    generator.setCount('multiple_choice', 99);
    expect(generator.total()).toBe(2);
    generator.generate();
    await vi.waitFor(() => expect(generate).toHaveBeenCalled());
    expect(generate).toHaveBeenCalledWith(expect.objectContaining({ counts: { short_answer: 2 } }));
  });

  it('sends nothing when the counts add up to more than ten', () => {
    const { generator, generate } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.setCount('multiple_choice', 9);
    generator.generate();
    expect(generator.note()).toBe('Ask for at most 10 questions in total; you have 11.');
    expect(generate).not.toHaveBeenCalled();
  });

  it('sends the request, refreshes the allowance, and adds only the drafts the teacher keeps', async () => {
    const { generator, generate, refresh, kept } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.toggleType('free_response', true);
    generator.setCount('multiple_choice', 4);
    generator.setCount('free_response', 1);
    generator.focus.setValue('  unit tests  ');
    generator.existingPrompts = ['  Already here ', ''];
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    expect(generate).toHaveBeenCalledWith({
      classroom_id: 'c1',
      document_ids: ['d1'],
      topic_id: 's1',
      counts: { multiple_choice: 4, short_answer: 2, free_response: 1 },
      difficulty: 'intermediate',
      focus: 'unit tests',
      existing_prompts: ['Already here'],
      review: true,
    });
    expect(refresh).toHaveBeenCalled();
    expect(generator.reviewing()).toBe(true);
    expect(generator.dropped()).toMatch(/^1 draft was left out/);

    generator.add(0);
    generator.add(0);
    generator.discard(1);
    expect(kept.map((q) => q.prompt)).toEqual(['Which test checks one function?']);
    expect(kept[0].origin).toBe('generated');
    expect(generator.drafts()).toEqual([]);
  });

  it('skips the review when asked, and fixes the loading text at send time', async () => {
    const { generator, generate } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.skipReview.setValue(true);
    generator.generate();
    expect(generate).toHaveBeenCalledWith(expect.objectContaining({ review: false }));
    expect(generator.reviewing()).toBe(false);
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    generator.skipReview.setValue(false);
    expect(generator.reviewing()).toBe(false);
  });

  it('shows the disclosure only when there is something to explain', async () => {
    const { generator, generate } = setup();
    const clean = { generated: 2, passed_checks: 2, passed_review: 2, rejected: {} };
    generate.mockResolvedValueOnce(result({ rounds: [clean], model_calls: 3 }));
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    expect(generator.showRounds()).toBe(false);
    generate.mockResolvedValueOnce(result({ rounds: [clean, clean], model_calls: 5 }));
    generator.generate();
    await vi.waitFor(() => expect(generator.rounds()).toHaveLength(2));
    expect(generator.showRounds()).toBe(true);
    expect(generator.modelCalls()).toBe(5);
    generate.mockResolvedValueOnce(result());
    generator.generate();
    await vi.waitFor(() => expect(generator.rounds()).toHaveLength(0));
    expect(generator.showRounds()).toBe(false);
  });

  it('words the result per type when several were asked for, and as before for one', async () => {
    const { generator, generate } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    expect(generator.drafted()).toBe('2 of 3 questions drafted.');
    const short = draft('Name the test.');
    short.question.type = 'short_answer';
    generate.mockResolvedValueOnce(
      result({
        questions: [draft('One?'), draft('Two?'), draft('Three?'), short],
        requested: 5,
        requested_by_type: { multiple_choice: 3, short_answer: 2 },
        returned: 4,
      }),
    );
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(4));
    expect(generator.drafted()).toBe('Drafted: Multiple choice 3 of 3 · Short answer 1 of 2.');
  });

  it('keeps nothing when the assessment is full', async () => {
    const { generator, kept } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    generator.room = false;
    generator.add(0);
    expect(kept).toEqual([]);
    expect(generator.drafts()).toHaveLength(2);
  });

  it('moves focus to the next draft, to Discard when the assessment is full, then back to Generate', async () => {
    vi.useFakeTimers();
    const { generator } = setup();
    for (const id of ['generated-0-add', 'generated-1-add', 'generated-1-discard', 'generate-button']) {
      const button = document.createElement('button');
      button.id = id;
      document.body.append(button);
    }
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    generator.discard(0);
    vi.runAllTimers();
    expect(document.activeElement?.id).toBe('generated-1-add');
    generator.room = false;
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    generator.discard(0);
    vi.runAllTimers();
    expect(document.activeElement?.id).toBe('generated-1-discard');
    generator.discard(1);
    vi.runAllTimers();
    expect(document.activeElement?.id).toBe('generate-button');
    document.body.replaceChildren();
    vi.useRealTimers();
  });

  it('shows a failure and still refreshes the allowance', async () => {
    const { generator, generate, refresh } = setup();
    generate.mockRejectedValueOnce(new Error('You have used today’s AI allowance.'));
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.result.error()).toBe('You have used today’s AI allowance.'));
    expect(refresh).toHaveBeenCalled();
  });
});

describe('changing classroom', () => {
  it('drops the drafts and chosen documents', async () => {
    const { generator } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    generator.classroomId = 'c2';
    generator.ngOnChanges({ classroomId: new SimpleChange('c1', 'c2', false) });
    expect(generator.drafts()).toEqual([]);
    expect(generator.chosen()).toEqual([]);
  });
});
