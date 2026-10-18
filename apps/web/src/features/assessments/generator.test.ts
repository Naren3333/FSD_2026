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
import { QuestionGeneratorComponent, droppedSummary, sourceDocuments } from './generator';

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
    generator.count.setValue(11);
    generator.generate();
    expect(generator.note()).toBe('Ask for 1–10 questions.');
    expect(generate).not.toHaveBeenCalled();
  });

  it('sends the request, refreshes the allowance, and adds only the drafts the teacher keeps', async () => {
    const { generator, generate, refresh, kept } = setup();
    generator.toggleDocument('d1', true);
    generator.topic.setValue('s1');
    generator.toggleType('free_response', true);
    generator.focus.setValue('  unit tests  ');
    generator.existingPrompts = ['  Already here ', ''];
    generator.generate();
    await vi.waitFor(() => expect(generator.drafts()).toHaveLength(2));
    expect(generate).toHaveBeenCalledWith({
      classroom_id: 'c1',
      document_ids: ['d1'],
      topic_id: 's1',
      question_types: ['multiple_choice', 'short_answer', 'free_response'],
      difficulty: 'intermediate',
      count: 5,
      focus: 'unit tests',
      existing_prompts: ['Already here'],
    });
    expect(refresh).toHaveBeenCalled();
    expect(generator.dropped()).toMatch(/^1 draft was left out/);

    generator.add(0);
    generator.add(0);
    generator.discard(1);
    expect(kept.map((q) => q.prompt)).toEqual(['Which test checks one function?']);
    expect(kept[0].origin).toBe('generated');
    expect(generator.drafts()).toEqual([]);
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
