// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { QuestionGenerationApi } from '../../api/clients';
import type { DocumentRecord, Guidance } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { Section } from '../../shared/section';
import { excerptLabel, questionForm } from './editor';
import { MarkingGuidanceComponent, mergeAccepted } from './guidance';

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
const shortGuidance: Guidance = {
  type: 'short_answer',
  accepted_answers: ['unit test', 'a unit test'],
  rubric: null,
  marking_points: { text: '1 mark: names a unit test.' },
  sources: [{ document_id: 'd1', chunk_id: 'k1', page: 2, text: 'A unit test checks one function.' }],
  evidence: 'A unit test checks one function.',
  requires_teacher_review: true,
};
const writtenGuidance: Guidance = {
  ...shortGuidance,
  type: 'free_response',
  accepted_answers: [],
  rubric: {
    criteria: [
      {
        title: 'Explains isolation',
        levels: [
          { points: 0, descriptor: 'Not shown' },
          { points: 2, descriptor: 'Clear' },
        ],
      },
    ],
    model_answer: 'Unit tests isolate one function.',
  },
};

function setup(type: 'short_answer' | 'free_response', answer: Guidance) {
  const guidance = vi.fn(() => Promise.resolve(answer));
  const refresh = vi.fn();
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: QuestionGenerationApi, useValue: { guidance } },
      { provide: AiUsage, useValue: { refresh } },
    ],
  });
  TestBed.overrideComponent(MarkingGuidanceComponent, { set: { template: '', imports: [] } });
  const component = TestBed.createComponent(MarkingGuidanceComponent).componentInstance;
  const documents = new Section<DocumentRecord[]>();
  documents.set([
    doc({ id: 'scheme', doc_type: 'marking_scheme' }),
    doc({}),
    doc({ id: 'wait', status: 'indexing' }),
  ]);
  const question = questionForm();
  question.controls.type.setValue(type);
  question.controls.points.setValue(2);
  component.question = question;
  component.documents = documents;
  component.classroomId = 'c1';
  return { component, question, guidance, refresh };
}

describe('guidance helpers', () => {
  it('adds suggested answers after the teacher’s own, without repeats', () => {
    expect(mergeAccepted('unit test\n', ['unit test', ' a unit test '])).toBe('unit test\na unit test');
    expect(
      mergeAccepted(
        '',
        Array.from({ length: 30 }, (_, i) => `a${i}`),
      ).split('\n'),
    ).toHaveLength(20);
  });
});

describe('drafting marking guidance', () => {
  it('needs the question first', () => {
    const { component, guidance } = setup('short_answer', shortGuidance);
    component.draft();
    expect(component.note()).toMatch(/Write the question first/);
    expect(guidance).not.toHaveBeenCalled();
  });

  it('drafts from the teaching materials and changes nothing until used', async () => {
    const { component, question, guidance, refresh } = setup('short_answer', shortGuidance);
    question.controls.prompt.setValue('Which test checks one function?');
    question.controls.accepted.setValue('unit test');
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(shortGuidance));
    expect(guidance).toHaveBeenCalledWith({
      classroom_id: 'c1',
      document_ids: ['d1'],
      prompt: 'Which test checks one function?',
      type: 'short_answer',
      points: 2,
    });
    expect(refresh).toHaveBeenCalled();
    expect(question.controls.marking_scheme.value).toEqual([]);

    component.use();
    component.use();
    expect(question.controls.accepted.value).toBe('unit test\na unit test');
    expect(question.controls.marking_scheme.value).toEqual([{ text: '1 mark: names a unit test.' }]);
    expect(question.dirty).toBe(true);
    expect(component.used()).toBe(true);
    expect(
      excerptLabel(question.controls.marking_scheme.value[0], [], question.controls.marking_scheme),
    ).toBe('Written by AI');
    expect(component.source(shortGuidance.sources[0])).toBe('Testing slides, page 2');
  });

  it('replaces a written response’s rubric and model answer', async () => {
    const { component, question, guidance } = setup('free_response', writtenGuidance);
    question.controls.prompt.setValue('Why isolate unit tests?');
    component.document.setValue('scheme');
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(writtenGuidance));
    expect(guidance).toHaveBeenCalledWith(
      expect.objectContaining({ document_ids: ['scheme'], type: 'free_response' }),
    );
    expect(guidance.mock.calls[0]).not.toHaveProperty('0.points');
    component.use();
    expect(question.controls.criteria.getRawValue()).toEqual([
      { title: 'Explains isolation', levels: '0: Not shown\n2: Clear' },
    ]);
    expect(question.controls.model_answer.value).toBe('Unit tests isolate one function.');
  });

  it('is not used once the question’s type has changed', async () => {
    const { component, question } = setup('short_answer', shortGuidance);
    question.controls.prompt.setValue('Which test checks one function?');
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(shortGuidance));
    question.controls.type.setValue('free_response');
    component.use();
    expect(component.used()).toBe(false);
    expect(question.controls.marking_scheme.value).toEqual([]);
  });

  it('is not used once the question has been reworded', async () => {
    const { component, question } = setup('short_answer', shortGuidance);
    question.controls.prompt.setValue('Which test checks one function?');
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(shortGuidance));
    question.controls.prompt.setValue('Which test checks a whole system?');
    expect(component.stale()).toBe(true);
    component.use();
    expect(component.used()).toBe(false);
    expect(question.controls.accepted.value).toBe('');
  });

  it('adds no marking points when the assessment has all the passages it can', async () => {
    const { component, question } = setup('short_answer', shortGuidance);
    question.controls.prompt.setValue('Which test checks one function?');
    component.assessmentPassages = 20;
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(shortGuidance));
    component.use();
    expect(question.controls.marking_scheme.value).toEqual([]);
    expect(question.controls.accepted.value).toBe('unit test\na unit test');
  });

  it('adds no marking points to a full marking scheme', async () => {
    const { component, question } = setup('short_answer', shortGuidance);
    question.controls.prompt.setValue('Which test checks one function?');
    const full = Array.from({ length: 5 }, (_, i) => ({ text: `Passage ${i}` }));
    question.controls.marking_scheme.setValue(full);
    component.draft();
    await vi.waitFor(() => expect(component.result.value()).toEqual(shortGuidance));
    expect(component.schemeFull()).toBe(true);
    component.use();
    expect(question.controls.marking_scheme.value).toEqual(full);
    expect(question.controls.accepted.value).toBe('unit test\na unit test');
  });
});
