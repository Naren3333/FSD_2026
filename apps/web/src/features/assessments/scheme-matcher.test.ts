// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { FormArray } from '@angular/forms';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ContentApi } from '../../api/clients';
import type { DocumentRecord, SearchHit, SearchRequest } from '../../api/models';
import { Section } from '../../shared/section';
import { questionForm, type QuestionForm } from './editor';
import { SchemeMatcherComponent, summary } from './scheme-matcher';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const hit = (chunk: string): SearchHit => ({
  chunk_id: chunk,
  document_id: 'scheme',
  title: 'Marking scheme',
  doc_type: 'marking_scheme',
  section_path: null,
  page_start: 1,
  page_end: 1,
  content: `Passage ${chunk}`,
});

function question(type: 'short_answer' | 'free_response' | 'numerical', prompt: string): QuestionForm {
  const q = questionForm();
  q.controls.type.setValue(type);
  q.controls.prompt.setValue(prompt);
  return q;
}

function setup(questions: QuestionForm[], answer: (r: SearchRequest) => SearchHit[]) {
  const search = vi.fn((r: SearchRequest) => Promise.resolve(answer(r)));
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: ContentApi, useValue: { search } }] });
  TestBed.overrideComponent(SchemeMatcherComponent, { set: { template: '', imports: [] } });
  const matcher = TestBed.createComponent(SchemeMatcherComponent).componentInstance;
  const documents = new Section<DocumentRecord[]>();
  documents.set([]);
  matcher.questions = new FormArray(questions);
  matcher.documents = documents;
  matcher.classroomId = 'c1';
  return { matcher, search };
}

describe('matching a marking scheme to the questions', () => {
  it('adds the best passage to each question a teacher marks, once', async () => {
    const qs = [question('short_answer', 'Q1'), question('numerical', 'Q2'), question('free_response', 'Q3')];
    const { matcher, search } = setup(qs, (r) => (r.query === 'Q1' ? [hit('k1')] : []));
    await matcher.match();
    expect(matcher.error()).toBe('Choose a marking scheme to match.');
    matcher.document.setValue('scheme');
    await matcher.match();
    expect(search).toHaveBeenCalledTimes(2);
    expect(search).toHaveBeenCalledWith({
      classroom_id: 'c1',
      query: 'Q1',
      document_ids: ['scheme'],
      top_k: 1,
    });
    expect(qs[0].controls.marking_scheme.value).toEqual([
      { document_id: 'scheme', chunk_id: 'k1', page: 1, text: 'Passage k1' },
    ]);
    expect(qs[2].controls.marking_scheme.value).toEqual([]);
    expect(matcher.message()).toBe(
      'Added a passage to 1 question. Check each one before saving. Nothing in the scheme matched 1 question.',
    );
    await matcher.match();
    expect(qs[0].controls.marking_scheme.value).toHaveLength(1);
    expect(matcher.message()).toMatch(/1 question already had the best match/);
  });

  it('stops at the assessment’s limit of twenty passages', async () => {
    const qs = Array.from({ length: 5 }, (_, i) => question('short_answer', `Q${i}`));
    for (const q of qs.slice(0, 4))
      q.controls.marking_scheme.setValue(Array.from({ length: 5 }, (_, n) => ({ text: `p${n}` })));
    const { matcher, search } = setup(qs, () => [hit('k9')]);
    matcher.document.setValue('scheme');
    await matcher.match();
    expect(search).not.toHaveBeenCalled();
    expect(qs[4].controls.marking_scheme.value).toEqual([]);
    expect(matcher.message()).toBe('No passage was added. 5 questions had no room for another passage.');
  });

  it('keeps what was added when a search fails', async () => {
    const qs = [question('short_answer', 'Q1'), question('short_answer', 'Q2')];
    const { matcher, search } = setup(qs, () => [hit('k1')]);
    search.mockResolvedValueOnce([hit('k1')]).mockRejectedValueOnce(new Error('Search is unavailable.'));
    matcher.document.setValue('scheme');
    await matcher.match();
    expect(qs[0].controls.marking_scheme.value).toHaveLength(1);
    expect(matcher.error()).toBe(
      'Search is unavailable. Added a passage to 1 question. Check each one before saving.',
    );
    expect(matcher.busy()).toBe(false);
  });

  it('stops when the teacher moves to another classroom mid-match', async () => {
    const qs = [question('short_answer', 'Q1'), question('short_answer', 'Q2')];
    const { matcher, search } = setup(qs, () => [hit('k1')]);
    search.mockImplementationOnce(() => {
      matcher.classroomId = 'c2';
      return Promise.resolve([hit('k1')]);
    });
    matcher.document.setValue('scheme');
    await matcher.match();
    expect(search).toHaveBeenCalledTimes(1);
    expect(qs.map((q) => q.controls.marking_scheme.value)).toEqual([[], []]);
    expect(matcher.questions.dirty).toBe(false);
    expect(matcher.busy()).toBe(false);
  });

  it('needs a written question first', async () => {
    const { matcher, search } = setup([question('numerical', 'Q1')], () => []);
    matcher.document.setValue('scheme');
    await matcher.match();
    expect(matcher.error()).toMatch(/Write a short-answer or written-response question first/);
    expect(search).not.toHaveBeenCalled();
    expect(summary({ added: 2, unmatched: 0, full: 0, already: 0 })).toBe(
      'Added a passage to 2 questions. Check each one before saving.',
    );
  });
});
