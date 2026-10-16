import { describe, expect, it } from 'vitest';
import type { Question } from '../../api/models';
import {
  assessmentForm,
  dueState,
  fillForm,
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
    q.patchValue({ prompt: 'Gas?', skill_id: 's1', type: 'short_answer' });
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
      skill_id: 's1',
      type: 'short_answer',
      accepted: 'oxygen\n',
      points: 2,
    });
    const free = questionForm();
    free.patchValue({ prompt: 'Explain.', skill_id: 's1', type: 'free_response', max_length: 800 });
    free.controls.criteria.at(0).patchValue({ title: 'Accuracy', levels: '0: Wrong\n4: Right' });
    form.controls.questions.push(free);
    form.controls.ai_marking.setValue(true);
    const sent = toAssessment(form, 'c1', 'draft');
    expect(sent.status).toBe('draft');
    expect(sent.ai_marking).toBe(true);
    expect(sent.questions[0]).toEqual({
      prompt: 'Gas?',
      type: 'short_answer',
      skill_id: 's1',
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
      skill_id: 's1',
      max_length: 900,
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
    expect(form.dirty).toBe(false);
  });
});

describe('dates', () => {
  it('round-trips a local date input', () => {
    expect(isoOrNull(localInput('2026-10-20T07:30:00.000Z'))).toBe('2026-10-20T07:30:00.000Z');
    expect(isoOrNull('')).toBeNull();
    expect(localInput(null)).toBe('');
  });

  it('says where a student stands with the due date', () => {
    const now = new Date('2026-10-16T00:00:00Z');
    expect(dueState(null, 'reject', now)).toBe('none');
    expect(dueState('2026-10-17T00:00:00Z', 'reject', now)).toBe('upcoming');
    expect(dueState('2026-10-15T00:00:00Z', 'accept_flagged', now)).toBe('late');
    expect(dueState('2026-10-15T00:00:00Z', 'reject', now)).toBe('closed');
  });
});
