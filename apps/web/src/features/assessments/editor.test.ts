import { describe, expect, it } from 'vitest';
import type { DraftQuestion, Question } from '../../api/models';
import {
  MAX_EXCERPT_TEXT,
  assessmentForm,
  clipText,
  draftQuestion,
  fillForm,
  fromAgent,
  isoOrNull,
  localInput,
  parseLevels,
  questionForm,
  rubricTotal,
  toAssessment,
} from './editor';

describe('rubric levels', () => {
  it('reads one "points: description" level per line', () => {
    expect(parseLevels('0: None\n 2 | Some \n\n4 - All')).toEqual([
      { points: 0, descriptor: 'None' },
      { points: 2, descriptor: 'Some' },
      { points: 4, descriptor: 'All' },
    ]);
    expect(parseLevels('Full marks')).toBeNull();
  });

  it('adds up each criterion’s top level', () => {
    expect(rubricTotal([{ levels: '0: a\n4: b' }, { levels: '1: c\n0: d' }])).toBe(5);
  });

  it('refuses one level, repeated points or more than 100 points', () => {
    const q = questionForm();
    q.controls.type.setValue('free_response');
    const levels = q.controls.criteria.at(0).controls.levels;
    for (const bad of ['3: only', '1: a\n1: b', '0: a\n101: b']) {
      levels.setValue(bad);
      expect(levels.valid).toBe(false);
    }
    levels.setValue('0: a\n3: b');
    expect(levels.valid).toBe(true);
  });
});

describe('question form', () => {
  it('validates only the fields its type uses', () => {
    const q = questionForm();
    q.patchValue({ prompt: 'Gas?', topic_id: 's1', type: 'short_answer' });
    expect(q.controls.answer.enabled).toBe(false);
    expect(q.valid).toBe(false); // no accepted answers yet
    q.controls.accepted.setValue('oxygen\nO2');
    expect(q.valid).toBe(true);
    q.controls.type.setValue('multiple_choice');
    expect(q.controls.accepted.enabled).toBe(false);
    expect(q.valid).toBe(false); // needs options and a key
  });
});

describe('assessment conversion', () => {
  it('sends each type with only its own fields, and free-response points from the rubric', () => {
    const form = assessmentForm();
    form.controls.title.setValue('Plants');
    const [short] = form.controls.questions.controls;
    short.patchValue({
      prompt: 'Gas?',
      topic_id: 's1',
      type: 'short_answer',
      accepted: 'oxygen\n',
      points: 2,
    });
    const free = questionForm();
    free.patchValue({ prompt: 'Explain.', topic_id: 's1', type: 'free_response', max_length: 800 });
    free.controls.criteria.at(0).patchValue({ title: 'Accuracy', levels: '0: Wrong\n4: Right' });
    form.controls.questions.push(free);
    form.controls.ai_marking.setValue(true);
    const sent = toAssessment(form, 'c1', 'draft');
    expect(sent.status).toBe('draft');
    expect(sent.ai_marking).toBe(true);
    expect(sent.questions[0]).toEqual({
      prompt: 'Gas?',
      type: 'short_answer',
      topic_id: 's1',
      options: [],
      answer: '',
      tolerance: '',
      accepted_answers: ['oxygen'],
      case_sensitive: false,
      points: 2,
    });
    expect(sent.questions[1]).toMatchObject({
      type: 'free_response',
      max_length: 800,
      points: 4,
      rubric: {
        criteria: [
          {
            title: 'Accuracy',
            levels: [
              { points: 0, descriptor: 'Wrong' },
              { points: 4, descriptor: 'Right' },
            ],
          },
        ],
      },
    });
    expect(sent.questions[1].rubric).not.toHaveProperty('model_answer');
    expect(sent.questions[1]).not.toHaveProperty('marking_scheme');
  });

  it('sends marking points only with the questions a teacher marks', () => {
    const form = assessmentForm();
    form.controls.title.setValue('Plants');
    const [q] = form.controls.questions.controls;
    const passage = { document_id: 'd1', chunk_id: 'k1', page: 2, text: 'Accept O2.' };
    q.patchValue({ prompt: 'Gas?', topic_id: 's1', type: 'short_answer', accepted: 'oxygen' });
    q.controls.marking_scheme.setValue([passage]);
    expect(toAssessment(form, 'c1', 'draft').questions[0].marking_scheme).toEqual([passage]);
    // A numerical question keeps no marking points, even ones added while it was a short answer.
    q.patchValue({ type: 'numerical', answer: '1' });
    expect(q.controls.marking_scheme.enabled).toBe(false);
    expect(toAssessment(form, 'c1', 'draft').questions[0]).not.toHaveProperty('marking_scheme');
  });

  it('allows five marking points a question and twenty an assessment', () => {
    const form = assessmentForm();
    const q = form.controls.questions.at(0);
    q.controls.type.setValue('free_response');
    const scheme = q.controls.marking_scheme;
    scheme.setValue(Array.from({ length: 6 }, () => ({ text: 'x' })));
    expect(scheme.valid).toBe(false);
    scheme.setValue([{ text: '  ' }]);
    expect(scheme.valid).toBe(false);
    scheme.setValue([{ text: 'é'.repeat(MAX_EXCERPT_TEXT) }]);
    expect(scheme.valid).toBe(true);
    for (let i = 0; i < 4; i++) {
      const more = questionForm();
      more.controls.type.setValue('short_answer');
      more.controls.marking_scheme.setValue(Array.from({ length: 5 }, () => ({ text: 'x' })));
      form.controls.questions.push(more);
    }
    expect(form.controls.questions.hasError('schemeTotal')).toBe(true);
    form.controls.questions.removeAt(4);
    expect(form.controls.questions.hasError('schemeTotal')).toBe(false);
  });

  it('cuts text by characters, not UTF-16 units', () => {
    expect(clipText('😀😀😀', 2)).toBe('😀😀');
    expect(clipText('abc', 5)).toBe('abc');
  });

  it('turns AI marking off when no question is teacher-marked', () => {
    const form = assessmentForm();
    form.controls.ai_marking.setValue(true);
    expect(toAssessment(form, 'c1', 'published').ai_marking).toBe(false);
  });

  it('fills the editor with a draft, rubric included', () => {
    const form = assessmentForm();
    const question: Question = {
      id: 'q1',
      prompt: 'Explain.',
      type: 'free_response',
      options: [],
      points: 3,
      topic_id: 's1',
      max_length: 900,
      marking_scheme: [{ text: 'Clarity needs an example.' }],
      rubric: {
        criteria: [
          {
            id: 'c1',
            title: 'Clarity',
            levels: [
              { points: 0, descriptor: 'Unclear' },
              { points: 3, descriptor: 'Clear' },
            ],
          },
        ],
      },
    };
    fillForm(form, {
      id: 'a1',
      classroom_id: 'c1',
      title: 'Draft',
      questions: [question],
      due_at: '2026-10-20T07:00:00Z',
      release_policy: 'manual',
    });
    expect(form.controls.title.value).toBe('Draft');
    expect(form.controls.release_policy.value).toBe('manual');
    expect(form.controls.questions.at(0).controls.criteria.at(0).controls.levels.value).toBe(
      '0: Unclear\n3: Clear',
    );
    expect(isoOrNull(form.controls.due_at.value)).toBe('2026-10-20T07:00:00.000Z');
    expect(form.controls.questions.at(0).controls.marking_scheme.value).toEqual([
      { text: 'Clarity needs an example.' },
    ]);
    expect(form.dirty).toBe(false);
  });
});

describe('dates', () => {
  it('round-trips a local date input', () => {
    expect(isoOrNull(localInput('2026-10-20T07:30:00.000Z'))).toBe('2026-10-20T07:30:00.000Z');
    expect(isoOrNull('')).toBeNull();
    expect(localInput(null)).toBe('');
  });
});

describe('generated questions', () => {
  it('keep their sources and origin through the editor', () => {
    const form = assessmentForm();
    const generated: Omit<Question, 'id'> = {
      prompt: 'Which test checks one function?',
      type: 'multiple_choice',
      options: ['Unit', 'End-to-end'],
      answer: 'Unit',
      points: 1,
      topic_id: 's1',
      sources: [{ document_id: 'd1', chunk_id: 'k1', page: 2, text: 'A unit test checks one function.' }],
      origin: 'generated',
    };
    form.controls.questions.setControl(0, questionForm(generated));
    form.controls.questions.push(questionForm());
    const [kept, written] = toAssessment(form, 'c1', 'draft').questions;
    expect(kept.origin).toBe('generated');
    expect(kept.sources).toEqual(generated.sources);
    expect(written).not.toHaveProperty('origin');
    expect(written).not.toHaveProperty('sources');
  });

  it('drop the nulls the agent sends for what it leaves out', () => {
    const draft: DraftQuestion = {
      prompt: 'Explain why a unit test is fast.',
      type: 'free_response',
      topic_id: 's1',
      points: 2,
      options: [],
      answer: '',
      rubric: null,
      max_length: null,
      marking_scheme: [{ document_id: null, chunk_id: null, page: null, text: 'Names isolation.' }],
      sources: [{ document_id: 'd1', chunk_id: 'k1', page: 2, text: 'A unit test checks one function.' }],
      origin: 'generated',
    };
    const kept = draftQuestion(draft);
    expect(kept).not.toHaveProperty('rubric');
    expect(kept).not.toHaveProperty('max_length');
    expect(kept.marking_scheme).toEqual([{ text: 'Names isolation.' }]);
    expect(kept.sources).toEqual(draft.sources);
    expect(fromAgent({ text: 'x', page: null })).toEqual({ text: 'x' });
  });
});
