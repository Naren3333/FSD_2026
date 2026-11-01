// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi } from '../../api/clients';
import type { Assessment, BankQuestion, DocumentRecord, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ActionState } from '../../shared/action-state';
import { Section } from '../../shared/section';
import { BankPickerComponent } from './bank-picker';
import { assessmentForm, fillForm, forBank, fromBank, questionForm, syncSaved, toAssessment } from './editor';
import { AssessmentEditorComponent } from './editor-view';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

/* the question bank in the editor (adr 0017). */

const banked: BankQuestion = {
  id: 'b1',
  prompt: '2 + 2?',
  type: 'numerical',
  options: [],
  answer: '4',
  tolerance: '0',
  points: 2,
  topic_id: 's1',
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
};

function stored(overrides: Partial<Assessment> = {}): Assessment {
  return {
    id: 'a1',
    classroom_id: 'c1',
    title: 'Sums',
    opens_at: null,
    due_at: null,
    late_policy: 'accept_flagged',
    status: 'draft',
    questions: [
      {
        id: 'q1',
        prompt: 'Capital?',
        type: 'short_answer',
        options: [],
        answer: '',
        tolerance: '',
        accepted_answers: ['Paris'],
        points: 1,
        topic_id: 's1',
        marking_scheme: [{ text: 'Paris only' }],
        sources: [{ text: 'France is in Europe.' }],
        bank_question_id: 'b9',
      },
    ],
    ...overrides,
  };
}

describe('the editor form and the bank', () => {
  it('keeps each question’s link and sends it back, but never its id', () => {
    const form = assessmentForm();
    fillForm(form, stored());
    const sent = toAssessment(form, 'c1', 'draft').questions[0];
    expect(sent.bank_question_id).toBe('b9');
    expect('id' in sent).toBe(false);
  });

  it('takes the stored ids and links after a save', () => {
    const form = assessmentForm();
    fillForm(form, stored());
    syncSaved(
      form,
      stored({ questions: [{ ...stored().questions[0], id: 'q2', bank_question_id: undefined }] }),
    );
    const q = form.controls.questions.at(0).controls;
    expect(q.id.value).toBe('q2');
    expect(q.bank_question_id.value).toBe('');
    expect(form.dirty).toBe(false);
  });

  it('adds a bank question as a linked copy, and sends a question to the bank without its passages', () => {
    expect(fromBank(banked)).toEqual({
      prompt: '2 + 2?',
      type: 'numerical',
      options: [],
      answer: '4',
      tolerance: '0',
      points: 2,
      topic_id: 's1',
      bank_question_id: 'b1',
    });
    const form = assessmentForm();
    fillForm(form, stored());
    const kept = forBank(form.controls.questions.at(0));
    expect(kept).toMatchObject({ prompt: 'Capital?', accepted_answers: ['Paris'] });
    expect(Object.keys(kept)).not.toContain('marking_scheme');
    expect(Object.keys(kept)).not.toContain('sources');
    expect(Object.keys(kept)).not.toContain('bank_question_id');
  });
});

function editorSetup(archived: Topic[] = []) {
  const api = {
    saveToBank: vi.fn(() => Promise.resolve({ ...banked, id: 'b2' })),
    updateBankQuestion: vi.fn(() => Promise.resolve(banked)),
    addToBank: vi.fn(() => Promise.resolve({ ...banked, id: 'b3' })),
  };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: AssessmentApi, useValue: api },
      {
        provide: CurriculumNames,
        useValue: { topics: signal([]), archived: signal(archived), topicName: () => 'Fractions' },
      },
    ],
  });
  TestBed.overrideComponent(AssessmentEditorComponent, { set: { template: '', imports: [] } });
  const editor = TestBed.createComponent(AssessmentEditorComponent).componentInstance;
  const form = assessmentForm();
  editor.form = form;
  editor.state = new ActionState();
  editor.classroomId = 'c1';
  editor.documents = new Section<DocumentRecord[]>();
  editor.topicsState = new Section<Topic[]>();
  return { editor, form, api };
}

describe('saving a question to the bank from the editor', () => {
  it('waits for the assessment to be saved, then saves and links the question', async () => {
    const { editor, form, api } = editorSetup();
    fillForm(form, stored({ questions: [{ ...stored().questions[0], bank_question_id: undefined }] }));
    const question = form.controls.questions.at(0);
    expect(editor.canOfferBank(question)).toBe(false);
    editor.editing = stored();
    form.markAsDirty();
    expect(editor.canBank(question)).toBe(false);
    editor.saveToBank(question, 0);
    expect(api.saveToBank).not.toHaveBeenCalled();
    form.markAsPristine();
    editor.saveToBank(question, 0);
    await vi.waitFor(() => expect(question.controls.bank_question_id.value).toBe('b2'));
    expect(api.saveToBank).toHaveBeenCalledWith('a1', 'q1');
    expect(editor.state.message()).toBe('Question 1 is saved to your bank.');
    expect(form.dirty).toBe(false);
  });

  it('updates the linked bank question, or saves a new copy and keeps the link', async () => {
    const { editor, form, api } = editorSetup();
    fillForm(form, stored());
    editor.editing = stored();
    const question = form.controls.questions.at(0);
    editor.saveToBank(question, 0);
    await vi.waitFor(() => expect(api.updateBankQuestion).toHaveBeenCalled());
    expect(api.updateBankQuestion.mock.calls[0][0]).toBe('b9');
    await vi.waitFor(() => expect(editor.state.busy()).toBe(false));
    editor.saveAsNew(question, 0);
    await vi.waitFor(() => expect(api.addToBank).toHaveBeenCalled());
    expect(question.controls.bank_question_id.value).toBe('b9');
  });

  it('adds a bank question in place of an untouched one, and after the rest otherwise', () => {
    const { editor, form } = editorSetup();
    editor.addFromBank(fromBank(banked));
    expect(form.controls.questions.length).toBe(1);
    expect(form.controls.questions.at(0).controls.bank_question_id.value).toBe('b1');
    editor.addFromBank(fromBank({ ...banked, id: 'b4' }));
    expect(form.controls.questions.length).toBe(2);
    expect(editor.state.message()).toBe('Bank question added as question 2.');
    expect(form.dirty).toBe(true);
  });

  it('opens one panel at a time', () => {
    const { editor } = editorSetup();
    editor.toggleGenerator();
    editor.toggleBank();
    expect(editor.generatorOpen()).toBe(false);
    expect(editor.bankOpen()).toBe(true);
    editor.toggleGenerator();
    expect(editor.bankOpen()).toBe(false);
  });
});

describe('the bank picker', () => {
  function pickerSetup(archived: Topic[] = []) {
    const api = { bankPage: vi.fn(() => Promise.resolve({ items: [banked], total: 1 })) };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AssessmentApi, useValue: api },
        {
          provide: CurriculumNames,
          useValue: { topics: signal([]), archived: signal(archived), topicName: () => 'Fractions' },
        },
      ],
    });
    TestBed.overrideComponent(BankPickerComponent, { set: { template: '', imports: [] } });
    return { picker: TestBed.createComponent(BankPickerComponent).componentInstance, api };
  }

  it('reads the bank once when first opened, and again on a search with its filters', async () => {
    const { picker, api } = pickerSetup();
    picker.opened();
    picker.opened();
    await vi.waitFor(() => expect(picker.questions.value()).toEqual([banked]));
    expect(api.bankPage).toHaveBeenCalledTimes(1);
    picker.text.setValue('sum');
    picker.type.setValue('numerical');
    picker.search();
    expect(api.bankPage).toHaveBeenLastCalledWith({ topicId: '', type: 'numerical', text: 'sum' }, 0, 20);
  });

  it('adds a linked copy, but nothing on an archived topic or once the assessment is full', () => {
    const { picker } = pickerSetup();
    const added = vi.fn();
    picker.add.subscribe(added);
    picker.choose(banked);
    expect(added).toHaveBeenCalledWith(fromBank(banked));
    picker.room = false;
    picker.choose(banked);
    expect(added).toHaveBeenCalledTimes(1);
    const { picker: other } = pickerSetup([{ id: 's1', name: 'Old' } as Topic]);
    expect(other.archived(banked)).toBe(true);
  });
});

it('a fresh question form has no id or link', () => {
  const q = questionForm();
  expect(q.controls.id.value).toBe('');
  expect(q.controls.bank_question_id.value).toBe('');
});
