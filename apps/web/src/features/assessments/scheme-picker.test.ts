// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { FormControl } from '@angular/forms';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ContentApi } from '../../api/clients';
import type { DocumentRecord, Excerpt, SearchHit } from '../../api/models';
import { Section } from '../../shared/section';
import { MAX_EXCERPT_TEXT } from './editor';
import { SchemePickerComponent, excerptOf, schemeDocuments } from './scheme-picker';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: 'd1',
  classroom_id: 'c1',
  title: 'Mark scheme',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  doc_type: 'marking_scheme',
  ...change,
});
const hit = (change: Partial<SearchHit> = {}): SearchHit => ({
  chunk_id: 'k1',
  document_id: 'd1',
  title: 'Mark scheme',
  doc_type: 'marking_scheme',
  section_path: null,
  page_start: 3,
  page_end: 3,
  content: 'Award the mark for naming oxygen.',
  ...change,
});

describe('marking-scheme documents', () => {
  it('offers only indexed rubrics and marking schemes', () => {
    const docs = [
      doc({ id: 'scheme' }),
      doc({ id: 'rubric', doc_type: 'rubric' }),
      doc({ id: 'notes', doc_type: 'teaching_material' }),
      doc({ id: 'waiting', status: 'indexing' }),
      doc({ id: 'text', status: 'extracted', mime_type: 'text/plain' }),
    ];
    expect(schemeDocuments(docs).map((d) => d.id)).toEqual(['scheme', 'rubric']);
  });

  it('keeps where a passage came from, cut to the limit', () => {
    expect(excerptOf(hit())).toEqual({
      document_id: 'd1',
      chunk_id: 'k1',
      page: 3,
      text: 'Award the mark for naming oxygen.',
    });
    expect(Array.from(excerptOf(hit({ content: 'é'.repeat(MAX_EXCERPT_TEXT + 5) })).text)).toHaveLength(
      MAX_EXCERPT_TEXT,
    );
  });
});

function setup(prompt = 'Which gas do plants give off?') {
  const search = vi.fn(() => Promise.resolve([hit(), hit({ chunk_id: 'k2', page_start: 4, page_end: 5 })]));
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: ContentApi, useValue: { search } }] });
  TestBed.overrideComponent(SchemePickerComponent, { set: { template: '', imports: [] } });
  const fixture = TestBed.createComponent(SchemePickerComponent);
  const picker = fixture.componentInstance;
  const documents = new Section<DocumentRecord[]>();
  documents.set([doc({})]);
  picker.control = new FormControl<Excerpt[]>([], { nonNullable: true });
  picker.documents = documents;
  picker.classroomId = 'c1';
  picker.prompt = prompt;
  return { picker, search };
}

describe('the marking-scheme picker', () => {
  it('searches the chosen document for the question and keeps the passages the teacher adds', async () => {
    const { picker, search } = setup();
    picker.find();
    expect(picker.searchNote()).toBe('Choose a marking scheme to search.');
    picker.document.setValue('d1');
    picker.find();
    await vi.waitFor(() => expect(picker.results.value()).toHaveLength(2));
    expect(search).toHaveBeenCalledWith({
      classroom_id: 'c1',
      query: 'Which gas do plants give off?',
      document_ids: ['d1'],
      top_k: 5,
    });
    const [first] = picker.results.value()!;
    picker.add(first);
    picker.add(first);
    expect(picker.control.value).toEqual([excerptOf(first)]);
    expect(picker.added(first)).toBe(true);
    expect(picker.control.dirty).toBe(true);
    expect(picker.source(picker.control.value[0])).toBe('Mark scheme, page 3');
    expect(picker.pages(picker.results.value()![1])).toBe('pages 4–5');
  });

  it('asks for the question before searching', () => {
    const { picker, search } = setup('   ');
    picker.document.setValue('d1');
    picker.find();
    expect(search).not.toHaveBeenCalled();
    expect(picker.searchNote()).toMatch(/Write the question first/);
  });

  it('adds pasted passages up to the limit, and removes them', () => {
    const { picker } = setup();
    picker.pasted.setValue('  Accept O2.  ');
    picker.addPasted();
    expect(picker.control.value).toEqual([{ text: 'Accept O2.' }]);
    expect(picker.pasted.value).toBe('');
    expect(picker.source(picker.control.value[0])).toBe('Pasted passage');
    picker.pasted.setValue('x'.repeat(MAX_EXCERPT_TEXT + 1));
    picker.addPasted();
    expect(picker.control.value).toHaveLength(1);
    for (let i = 0; i < 6; i++) {
      picker.pasted.setValue(`Passage ${i}`);
      picker.addPasted();
    }
    expect(picker.control.value).toHaveLength(5);
    expect(picker.full()).toBe(true);
    picker.remove(0);
    expect(picker.control.value.map((e) => e.text)).toEqual([
      'Passage 0',
      'Passage 1',
      'Passage 2',
      'Passage 3',
    ]);
    expect(picker.full()).toBe(false);
  });

  it('shows a failed search and keeps the passages', async () => {
    const { picker, search } = setup();
    search.mockRejectedValueOnce(new Error('Search is unavailable.'));
    picker.control.setValue([{ text: 'Kept.' }]);
    picker.document.setValue('d1');
    picker.find();
    await vi.waitFor(() => expect(picker.results.error()).toBe('Search is unavailable.'));
    expect(picker.control.value).toEqual([{ text: 'Kept.' }]);
  });
});
