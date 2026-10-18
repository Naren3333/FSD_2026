import { describe, expect, it } from 'vitest';
import type { Evidence, Question } from '../../api/models';
import { applySuggestion, markingForm, marksOf, suggestionReason } from './marking';

const rubricQuestion: Question = {
  id: 'free',
  prompt: 'Explain.',
  type: 'free_response',
  options: [],
  points: 5,
  topic_id: 's1',
  rubric: {
    criteria: [
      {
        id: 'c1',
        title: 'Accuracy',
        levels: [
          { points: 0, descriptor: 'Wrong' },
          { points: 4, descriptor: 'Right' },
        ],
      },
      {
        id: 'c2',
        title: 'Clarity',
        levels: [
          { points: 0, descriptor: 'Unclear' },
          { points: 1, descriptor: 'Clear' },
        ],
      },
    ],
  },
};
const evidence: Evidence[] = [
  { question_id: 'auto', topic_id: 's1', correct: true, awarded: 1, possible: 1, marked_by: 'auto' },
  { question_id: 'short', topic_id: 's1', correct: false, awarded: 0, possible: 2, needs_marking: true },
  { question_id: 'free', topic_id: 's1', correct: false, awarded: 0, possible: 5, needs_marking: true },
];

describe('marking form', () => {
  it('requires marks for questions still to mark and keeps automatic marks', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    expect(form.controls['auto'].controls.awarded.value).toBe(1);
    expect(form.valid).toBe(false);
    form.controls['short'].controls.awarded.setValue(2);
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: 0 });
    expect(form.valid).toBe(true);
    form.controls['short'].controls.awarded.setValue(3);
    expect(form.valid).toBe(false); // above the question's marks
  });

  it('sends only what changed: new marks, overrides and feedback', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    expect(marksOf(form, evidence)).toEqual({});
    form.controls['short'].controls.awarded.setValue(2);
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: null });
    form.controls['auto'].controls.feedback.setValue('Well done.');
    // A rubric with a criterion left blank is not sent.
    expect(marksOf(form, evidence)).toEqual({ short: { awarded: 2 }, auto: { feedback: 'Well done.' } });
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: 1 });
    form.controls['auto'].controls.awarded.setValue(0);
    expect(marksOf(form, evidence)).toEqual({
      short: { awarded: 2 },
      auto: { awarded: 0, feedback: 'Well done.' },
      free: { criteria: { c1: 4, c2: 1 } },
    });
  });

  it('does not resend a rubric mark that is already saved', () => {
    const marked: Evidence[] = [
      {
        ...evidence[2],
        needs_marking: false,
        awarded: 5,
        criteria: [
          { criterion_id: 'c1', points: 4 },
          { criterion_id: 'c2', points: 1 },
        ],
      },
    ];
    const form = markingForm(marked, [rubricQuestion]);
    expect(form.controls['free'].controls.criteria.value).toEqual({ c1: 4, c2: 1 });
    expect(marksOf(form, marked)).toEqual({});
  });

  it('puts a suggestion into the form for the teacher to check', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    applySuggestion(form.controls['free'], {
      status: 'ready',
      awarded: 4,
      feedback: 'Say more about clarity.',
      criteria: [
        { criterion_id: 'c1', points: 4, evidence_quotes: ['light'], rationale: 'Right idea.' },
        { criterion_id: 'c2', points: 0, evidence_quotes: [], rationale: 'Hard to follow.' },
      ],
    });
    applySuggestion(form.controls['short'], { status: 'ready', awarded: 2 });
    expect(marksOf(form, evidence)).toEqual({
      short: { awarded: 2 },
      free: { criteria: { c1: 4, c2: 0 }, feedback: 'Say more about clarity.' },
    });
  });

  it('explains suggestions that are not ready', () => {
    expect(suggestionReason({ status: 'pending', awarded: 0 })).toBe('A suggestion is being prepared.');
    expect(suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_SPEND_LIMIT' })).toContain(
      'allowance',
    );
    expect(suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_BUSY' })).toContain(
      'other AI requests',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'invalid quote' })).toContain('yourself');
    expect(
      suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_PROVIDER_NOT_CONFIGURED' }),
    ).toContain('tried again');
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'MARKED_BY_TEACHER' })).toContain(
      'already marked',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'AI_OUTPUT_REJECTED' })).toContain(
      'yourself',
    );
  });
});
